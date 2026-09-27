"""回写路由：触发回写 / 查询状态 / 查看日志。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Contract, WritebackLog
from app.schemas import WritebackIn, WritebackOut, WritebackStatusOut
from app.services.approval import ApprovalError
from app.services.report_service import load_bundle
from app.services.writeback_service import (
    WritebackBlocked,
    get_writeback_status,
    write_back,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contracts", tags=["writeback"])


@router.post("/{contract_id}/writeback", response_model=WritebackOut, summary="写回审批意见")
def do_writeback(
    contract_id: int, body: WritebackIn, db: Session = Depends(get_db)
) -> WritebackOut:
    """把审查意见回写到审批系统。

    前置条件不满足 → 409（不可重试，需先修数据）；
    审批系统调用失败 → 200 + `status=failed`（可重试，前端据此提示）。
    这个区分是刻意的：把"业务不允许"和"外部系统抖动"分成两类。
    """
    bundle = load_bundle(db, contract_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"合同或任务不存在: {contract_id}")

    try:
        result = write_back(db, bundle, author=body.author, force=body.force)
    except WritebackBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ApprovalError as exc:
        # 适配层已在 write_back 内处理；这里兜底防漏
        raise HTTPException(status_code=502, detail=f"审批系统不可用: {exc}") from exc

    return WritebackOut(
        contract_id=result.contract_id, status=result.status, log_id=result.log_id,
        comment_id=result.comment_id, deduplicated=result.deduplicated,
        http_status=result.http_status, duration_ms=result.duration_ms,
        error_detail=result.error_detail,
    )


@router.get(
    "/{contract_id}/writeback/status",
    response_model=WritebackStatusOut,
    summary="回写状态（轮询）",
)
def writeback_status(contract_id: int, db: Session = Depends(get_db)) -> WritebackStatusOut:
    """回写状态。前端触发回写后轮询这个接口。"""
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    return WritebackStatusOut(**get_writeback_status(db, contract_id))


@router.get("/{contract_id}/writeback/logs", summary="回写日志")
def writeback_logs(contract_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """回写日志（审计）。"""
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    rows = db.execute(
        select(WritebackLog)
        .where(WritebackLog.contract_id == contract_id)
        .order_by(WritebackLog.created_at.desc())
    ).scalars()
    return [
        {
            "id": w.id, "status": w.status, "http_status": w.http_status,
            "retry_count": w.retry_count, "duration_ms": w.duration_ms,
            "error_detail": w.error_detail, "response": w.response,
            "created_at": w.created_at.isoformat(),
        }
        for w in rows
    ]
