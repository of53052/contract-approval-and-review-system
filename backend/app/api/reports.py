"""报告路由：预览 / 导出 / 下载。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.minio_client import get_minio
from app.models import Contract, ExportRecord
from app.schemas import ExportOut, ReportPreviewOut
from app.services.report_service import (
    export_report as export_report_file,
    load_bundle,
    render_markdown,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contracts", tags=["reports"])


@router.get("/{contract_id}/report/preview", response_model=ReportPreviewOut, summary="报告预览")
def preview_report(contract_id: int, db: Session = Depends(get_db)) -> ReportPreviewOut:
    """生成报告 Markdown 文本（不落存储）。前端用它做预览。"""
    bundle = load_bundle(db, contract_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"合同或任务不存在: {contract_id}")
    md = render_markdown(bundle)
    return ReportPreviewOut(
        contract_id=contract_id, markdown=md, char_count=len(md)
    )


@router.post("/{contract_id}/report/export", response_model=ExportOut, summary="导出报告")
def export_report(
    contract_id: int,
    db: Session = Depends(get_db),
    format: str = Query(
        default="markdown",
        description="导出格式：markdown（便于留痕/机读）或 pdf（精排，用于归档与发送）",
    ),
    created_by: str = Query(default="法务", description="导出人"),
) -> ExportOut:
    """生成报告并导出到 MinIO，登记 `export_record`。

    **两种格式并存**：Markdown 便于机读与差异比对，PDF 用于正式归档与发送。
    它们各自独立渲染（见 `report_service` / `report_pdf` 的说明），
    同一份数据导出两种格式，内容口径一致。
    """
    # 参数校验先于资源查询：非法格式是"请求本身不合法"，与合同是否存在无关，
    # 应先返回 422（否则合同不存在时会先撞上 404，错误信息误导调用方）。
    if format not in ("markdown", "pdf"):
        raise HTTPException(
            status_code=422, detail=f"不支持的导出格式: {format}（可选 markdown / pdf）"
        )

    bundle = load_bundle(db, contract_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"合同或任务不存在: {contract_id}")

    try:
        result = export_report_file(db, bundle, fmt=format, created_by=created_by)
    except Exception as exc:  # noqa: BLE001 - 对象存储/渲染故障转 502
        db.rollback()
        logger.error("报告导出失败（format=%s）: %s", format, exc)
        raise HTTPException(status_code=502, detail=f"报告导出失败: {exc}") from exc

    db.commit()
    return ExportOut(
        record_id=result.record_id, object_key=result.object_key,
        file_size=result.file_size, format=result.format,
        download_url=f"/api/contracts/{contract_id}/report/download/{result.record_id}",
    )


@router.get("/{contract_id}/report/download/{record_id}", summary="下载报告")
def download_report(
    contract_id: int, record_id: int, db: Session = Depends(get_db)
) -> Response:
    """下载报告。

    **后端代理 MinIO** 而不是返回预签名 URL：阶段一前端不直连对象存储，
    少一套跨域与凭据配置；代价是流量过后端，演示场景可接受。
    """
    record = db.get(ExportRecord, record_id)
    if record is None or record.contract_id != contract_id:
        raise HTTPException(status_code=404, detail=f"导出记录不存在: {record_id}")

    resp = None
    try:
        resp = get_minio().get_object(settings.minio_bucket_reports, record.file_object_key)
        data = resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.error("读取报告对象失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"对象存储不可用: {exc}") from exc
    finally:
        # resp 可能因 get_object 抛异常而未绑定，判空后再释放
        if resp is not None:
            try:
                resp.close()
                resp.release_conn()
            except Exception:  # noqa: BLE001 - 释放失败不影响已读数据
                pass

    filename = record.file_object_key.rsplit("/", 1)[-1]
    media = (
        "text/markdown; charset=utf-8" if record.format == "markdown"
        else "application/pdf"
    )
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{contract_id}/report/exports", summary="导出记录列表")
def list_exports(contract_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """该合同的导出历史。"""
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    rows = db.execute(
        select(ExportRecord)
        .where(ExportRecord.contract_id == contract_id)
        .order_by(ExportRecord.created_at.desc())
    ).scalars()
    return [
        {
            "id": e.id, "format": e.format, "object_key": e.file_object_key,
            "file_size": e.file_size, "created_by": e.created_by,
            "created_at": e.created_at.isoformat(),
            "download_url": f"/api/contracts/{contract_id}/report/download/{e.id}",
        }
        for e in rows
    ]
