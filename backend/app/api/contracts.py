"""合同路由：列表 / 详情 / 上传 / 条款 / 元数据 / 事件。"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.minio_client import get_minio, path_original
from app.models import (
    Anchor,
    Clause,
    Contract,
    ContractMetadata,
    ParseResult,
    ReviewTask,
    TaskEvent,
)
from app.models.enums import (
    BusinessType,
    ContractSource,
    FileFormat,
    TaskStatus,
    WritebackStatus,
)
from app.schemas import (
    AnchorOut,
    ClauseOut,
    ContractDetail,
    ContractListItem,
    MetadataOut,
    Page,
    TaskEventOut,
)
from app.services.parsing.dispatcher import detect_format

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contracts", tags=["contracts"])

#: 上传大小上限（字节）。阶段一是演示系统，20MB 足够覆盖示例合同。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _to_list_item(c: Contract, t: ReviewTask | None) -> ContractListItem:
    """合同 + 任务 → 列表项。任务缺失时给保守默认值。"""
    return ContractListItem(
        id=c.id, title=c.title, contract_no=c.contract_no,
        business_type=c.business_type, amount=c.amount, currency=c.currency,
        applicant=c.applicant, applicant_dept=c.applicant_dept,
        counterparty_name=c.counterparty_name,
        task_id=t.id if t else None,
        status=t.status if t else TaskStatus.PENDING.value,
        overall_risk=t.overall_risk if t else None,
        conclusion=t.conclusion if t else None,
        high_risk_count=t.high_risk_count if t else 0,
        medium_risk_count=t.medium_risk_count if t else 0,
        low_risk_count=t.low_risk_count if t else 0,
        writeback_status=t.writeback_status if t else WritebackStatus.NOT_WRITTEN.value,
        blocked_reason=t.blocked_reason if t else None,
        parsed_pages=t.parsed_pages if t else 0,
        total_pages=t.total_pages if t else None,
        created_at=c.created_at,
    )


@router.get("", response_model=Page[ContractListItem], summary="合同列表（大盘页）")
def list_contracts(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, description="按任务状态筛选"),
    risk: str | None = Query(default=None, description="按综合风险等级筛选"),
    business_type: str | None = Query(default=None, description="按业务类型筛选"),
    keyword: str | None = Query(default=None, description="按合同名称/编号模糊搜索"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Page[ContractListItem]:
    """合同列表。软删除的合同不返回。

    筛选条件作用在 `review_task` 的冗余字段上，避免列表页聚合查询（原则 P4）。
    """
    # 只查未删除的合同
    base = select(Contract, ReviewTask).outerjoin(
        ReviewTask, ReviewTask.contract_id == Contract.id
    ).where(Contract.deleted_at.is_(None))

    if status:
        base = base.where(ReviewTask.status == status)
    if risk:
        base = base.where(ReviewTask.overall_risk == risk)
    if business_type:
        base = base.where(Contract.business_type == business_type)
    if keyword:
        like = f"%{keyword}%"
        base = base.where(
            (Contract.title.like(like)) | (Contract.contract_no.like(like))
        )

    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar() or 0

    rows = db.execute(
        base.order_by(Contract.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return Page[ContractListItem](
        items=[_to_list_item(c, t) for c, t in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get("/{contract_id}", response_model=ContractDetail, summary="合同详情")
def get_contract(contract_id: int, db: Session = Depends(get_db)) -> ContractDetail:
    """合同详情。工作台进入时调它。"""
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    t = db.execute(
        select(ReviewTask).where(ReviewTask.contract_id == contract_id)
    ).scalar_one_or_none()
    item = _to_list_item(c, t)
    return ContractDetail(
        **item.model_dump(),
        file_format=c.file_format, file_name=c.file_name, file_size=c.file_size,
        pdf_object_key=c.pdf_object_key, source=c.source, external_id=c.external_id,
        summary=t.summary if t else None,
    )


@router.post("/upload", response_model=ContractDetail, summary="上传合同（手动入口）")
async def upload_contract(
    db: Session = Depends(get_db),
    file: UploadFile = File(description="合同文件：docx / pdf / 图片"),
    business_type: str = Form(default=BusinessType.PURCHASE.value),
    title: str | None = Form(default=None),
    applicant: str | None = Form(default=None),
    applicant_dept: str | None = Form(default=None),
    auto_review: bool = Form(default=True, description="上传后立即启动审查"),
) -> ContractDetail:
    """上传合同并（可选）立即触发审查。

    **去重**：`file_hash` 命中未删除的合同时直接返回已有记录，
    不重复落库也不重复解析（§13.3 幂等性）。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB",
        )

    file_name = file.filename or "unnamed"
    try:
        fmt = detect_format(file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    file_hash = hashlib.sha256(raw).hexdigest()
    existing = db.execute(
        select(Contract).where(
            Contract.file_hash == file_hash, Contract.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("上传命中去重: %s（合同 #%s）", file_name, existing.id)
        t = db.execute(
            select(ReviewTask).where(ReviewTask.contract_id == existing.id)
        ).scalar_one_or_none()
        item = _to_list_item(existing, t)
        return ContractDetail(
            **item.model_dump(),
            file_format=existing.file_format, file_name=existing.file_name,
            file_size=existing.file_size, pdf_object_key=existing.pdf_object_key,
            source=existing.source, external_id=existing.external_id,
            summary=t.summary if t else None,
        )

    if business_type not in {b.value for b in BusinessType}:
        raise HTTPException(status_code=400, detail=f"非法业务类型: {business_type}")

    contract = Contract(
        title=title or Path(file_name).stem,
        business_type=business_type,
        file_format=fmt.value,
        file_object_key=path_original(0, file_name.rsplit(".", 1)[-1]),  # 占位，落库后改
        file_name=file_name,
        file_size=len(raw),
        file_hash=file_hash,
        applicant=applicant,
        applicant_dept=applicant_dept,
        source=ContractSource.UPLOAD.value,
    )
    db.add(contract)
    db.flush()  # 取到 id 才能定 object key

    # 原件上传 MinIO：先上传成功再定 key，避免悬空引用
    ext = file_name.rsplit(".", 1)[-1].lower()
    key = path_original(contract.id, ext)
    try:
        get_minio().put_object(
            settings.minio_bucket_contracts,
            key,
            __import__("io").BytesIO(raw),
            length=len(raw),
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error("合同原件上传 MinIO 失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"对象存储不可用: {exc}") from exc

    contract.file_object_key = key
    task = ReviewTask(contract_id=contract.id, status=TaskStatus.PENDING.value)
    db.add(task)
    db.commit()
    db.refresh(contract)
    db.refresh(task)

    logger.info(
        "合同已上传: #%s %s（%s 字节，任务 #%s）",
        contract.id, file_name, len(raw), task.id,
    )

    if auto_review:
        _schedule_review(contract, task, raw, ext)

    item = _to_list_item(contract, task)
    return ContractDetail(
        **item.model_dump(),
        file_format=contract.file_format, file_name=contract.file_name,
        file_size=contract.file_size, pdf_object_key=contract.pdf_object_key,
        source=contract.source, external_id=contract.external_id,
        summary=task.summary,
    )


def _schedule_review(contract: Contract, task: ReviewTask, raw: bytes, ext: str) -> None:
    """把审查任务交给后台线程执行。

    **阶段一用进程内线程**（architecture.md §13.1）：演示并发量为 1，
    引 Celery 会多一个必须启动的服务。任务表设计成可迁移的。
    """
    import tempfile
    import threading

    from app.core.database import SessionLocal
    from app.services.llm import get_provider
    from app.workers.pipeline import Pipeline

    def _run() -> None:
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as fh:
                fh.write(raw)
                tmp_path = Path(fh.name)
            db2 = SessionLocal()
            try:
                t2 = db2.get(ReviewTask, task.id)
                if t2 is None:
                    return
                Pipeline(db2, t2, provider=get_provider()).run(tmp_path)
            finally:
                db2.close()
        except Exception:  # noqa: BLE001 - 后台线程不能把异常抛给请求
            logger.error("后台审查任务 #%s 异常", task.id, exc_info=True)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    threading.Thread(target=_run, name=f"review-{task.id}", daemon=True).start()


@router.get("/{contract_id}/clauses", response_model=list[ClauseOut], summary="条款列表")
def list_clauses(contract_id: int, db: Session = Depends(get_db)) -> list[ClauseOut]:
    """条款列表（工作台左栏渲染用）。按 seq 排序。"""
    _ensure_contract(db, contract_id)
    rows = db.execute(
        select(Clause).where(Clause.contract_id == contract_id).order_by(Clause.seq)
    ).scalars()
    return [
        ClauseOut(
            id=c.id, clause_type=c.clause_type, clause_no=c.clause_no, title=c.title,
            content=c.content, page_no=c.page_no, page_end=c.page_end,
            para_index=c.para_index, char_start=c.char_start, char_end=c.char_end,
            bbox_x0=c.bbox_x0, bbox_y0=c.bbox_y0, bbox_x1=c.bbox_x1, bbox_y1=c.bbox_y1,
            seq=c.seq, source=c.source,
        )
        for c in rows
    ]


@router.get("/{contract_id}/metadata", response_model=list[MetadataOut], summary="元数据列表")
def list_metadata(contract_id: int, db: Session = Depends(get_db)) -> list[MetadataOut]:
    """合同元数据。前端据此高亮"提取的元数据字段"。

    一次查完锚点，**避免 N+1**（同 `list_risks` 的处理）。
    """
    _ensure_contract(db, contract_id)
    rows = list(db.execute(
        select(ContractMetadata).where(ContractMetadata.contract_id == contract_id)
    ).scalars())
    if not rows:
        return []

    ids = [m.id for m in rows]
    anchors: dict[int, list[Anchor]] = {}
    for a in db.execute(
        select(Anchor).where(
            Anchor.owner_type == "contract_metadata", Anchor.owner_id.in_(ids)
        ).order_by(Anchor.owner_id, Anchor.seq)
    ).scalars():
        anchors.setdefault(a.owner_id, []).append(a)

    return [
        MetadataOut(
            id=m.id, meta_key=m.meta_key, meta_value=m.meta_value,
            value_normalized=m.value_normalized, value_type=m.value_type,
            confidence=m.confidence, need_review=bool(m.need_review),
            anchors=[
                AnchorOut(
                    id=a.id, page_no=a.page_no, bbox_x0=a.bbox_x0, bbox_y0=a.bbox_y0,
                    bbox_x1=a.bbox_x1, bbox_y1=a.bbox_y1, char_start=a.char_start,
                    char_end=a.char_end, quote_text=a.quote_text, source=a.source,
                    anchor_level=a.anchor_level, confidence=a.confidence,
                )
                for a in anchors.get(m.id, [])
            ],
        )
        for m in rows
    ]


@router.get("/{contract_id}/events", response_model=list[TaskEventOut], summary="任务事件轨迹")
def list_events(contract_id: int, db: Session = Depends(get_db)) -> list[TaskEventOut]:
    """任务事件（审计轨迹）。"""
    _ensure_contract(db, contract_id)
    task = db.execute(
        select(ReviewTask).where(ReviewTask.contract_id == contract_id)
    ).scalar_one_or_none()
    if task is None:
        return []
    rows = db.execute(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
    ).scalars()
    return [
        TaskEventOut(
            id=e.id, event_type=e.event_type, from_status=e.from_status,
            to_status=e.to_status, operator=e.operator, detail=e.detail,
            created_at=e.created_at,
        )
        for e in rows
    ]


@router.get("/{contract_id}/parse-results", summary="解析历史")
def list_parse_results(contract_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """解析历史。重试后 attempt 递增，用于解释"为什么页码变了"。"""
    _ensure_contract(db, contract_id)
    rows = db.execute(
        select(ParseResult)
        .where(ParseResult.contract_id == contract_id)
        .order_by(ParseResult.attempt)
    ).scalars()
    return [
        {
            "id": p.id, "attempt": p.attempt, "parse_method": p.parse_method,
            "converter_used": p.converter_used,
            "converter_fallback_chain": p.converter_fallback_chain,
            "page_count": p.page_count, "has_text_layer": bool(p.has_text_layer),
            "status": p.status, "duration_ms": p.duration_ms,
            "error_detail": p.error_detail, "created_at": p.created_at.isoformat(),
        }
        for p in rows
    ]


@router.get("/{contract_id}/pdf", summary="下载用于渲染的 PDF")
def download_contract_pdf(contract_id: int, db: Session = Depends(get_db)) -> Response:
    """下载合同 PDF（前端 PDF.js 渲染用）。

    **统一渲染入口**（architecture.md §6、§14.3）：
    - DOCX：返回 WPS COM 转换后的 PDF（`pdf_object_key`）
    - PDF：原件即 PDF，直接返回原件
    - 图片：阶段一未接入 OCR，返回 409 让前端提示

    **后端代理 MinIO**，理由同 `reports.download_report`：前端不直连对象存储。
    """
    c = _ensure_contract(db, contract_id)

    if c.file_format == FileFormat.PDF.value:
        bucket, key = settings.minio_bucket_contracts, c.file_object_key
    elif c.file_format == FileFormat.DOCX.value:
        if not c.pdf_object_key:
            raise HTTPException(
                status_code=409,
                detail="该 DOCX 尚未生成渲染用 PDF（转换可能失败或任务未完成）",
            )
        bucket, key = settings.minio_bucket_contracts, c.pdf_object_key
    else:
        raise HTTPException(
            status_code=409,
            detail=f"暂不支持渲染该格式: {c.file_format}（阶段一未接入 OCR）",
        )

    resp = None
    try:
        resp = get_minio().get_object(bucket, key)
        data = resp.read()
    except Exception as exc:  # noqa: BLE001 - 对象存储故障转 502
        logger.error("读取合同 PDF 失败: %s/%s | %s", bucket, key, exc)
        raise HTTPException(status_code=502, detail=f"对象存储不可用: {exc}") from exc
    finally:
        # resp 可能因 get_object 抛异常而未绑定，判空后再释放
        if resp is not None:
            try:
                resp.close()
                resp.release_conn()
            except Exception:  # noqa: BLE001 - 释放失败不影响已读数据
                pass

    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="contract-{contract_id}.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/{contract_id}/file", summary="下载合同原件")
def download_contract_original(contract_id: int, db: Session = Depends(get_db)) -> Response:
    """下载合同原件（用户核对用）。"""
    c = _ensure_contract(db, contract_id)
    resp = None
    try:
        resp = get_minio().get_object(settings.minio_bucket_contracts, c.file_object_key)
        data = resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.error("读取合同原件失败: %s | %s", c.file_object_key, exc)
        raise HTTPException(status_code=502, detail=f"对象存储不可用: {exc}") from exc
    finally:
        if resp is not None:
            try:
                resp.close()
                resp.release_conn()
            except Exception:  # noqa: BLE001
                pass

    # 文件名含中文，用 RFC 5987 编码，避免 latin-1 报错
    from urllib.parse import quote
    filename = c.file_name or f"contract-{contract_id}"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.delete("/{contract_id}", response_model=dict, summary="软删除合同")
def delete_contract(contract_id: int, db: Session = Depends(get_db)) -> dict:
    """软删除合同（原则 P6：软删除仅用于本表）。

    软删除后同一 `file_hash` 可重新上传（`deleted_key` 生成列兜底）。
    """
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    c.deleted_at = datetime.now()
    db.commit()
    logger.info("合同 #%s 已软删除", contract_id)
    return {"ok": True, "detail": f"合同 {contract_id} 已删除"}


def _ensure_contract(db: Session, contract_id: int) -> Contract:
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    return c
