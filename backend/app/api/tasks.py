"""任务路由：状态查询 / 进度轮询 / 重试 / 同步待办。"""

from __future__ import annotations

import io
import logging
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.minio_client import get_minio, path_original
from app.core.redis_client import get_redis, key_task_progress
from app.models import Contract, ReviewTask
from app.models.enums import TaskEventType, TaskStatus
from app.schemas import TaskOut, TaskProgressOut
from app.services.approval import ApprovalError, get_adapter
from app.workers import state_machine as sm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _to_task_out(t: ReviewTask) -> TaskOut:
    return TaskOut(
        id=t.id, contract_id=t.contract_id, status=t.status,
        writeback_status=t.writeback_status, blocked_reason=t.blocked_reason,
        blocked_detail=t.blocked_detail, overall_risk=t.overall_risk,
        conclusion=t.conclusion, summary=t.summary, total_pages=t.total_pages,
        parsed_pages=t.parsed_pages, high_risk_count=t.high_risk_count,
        medium_risk_count=t.medium_risk_count, low_risk_count=t.low_risk_count,
        parse_duration_ms=t.parse_duration_ms, review_duration_ms=t.review_duration_ms,
        started_at=t.started_at, blocked_at=t.blocked_at, completed_at=t.completed_at,
        version=t.version, created_at=t.created_at,
    )


@router.get("/{task_id}", response_model=TaskOut, summary="任务详情")
def get_task(task_id: int, db: Session = Depends(get_db)) -> TaskOut:
    t = db.get(ReviewTask, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _to_task_out(t)


@router.get("/{task_id}/progress", response_model=TaskProgressOut, summary="任务进度")
def get_progress(task_id: int, db: Session = Depends(get_db)) -> TaskProgressOut:
    """任务进度（前端轮询）。

    实时进度优先读 Redis（解析阶段高频写入），Redis 不可用或键已过期时
    退回数据库里的 `parsed_pages`——进度是辅助信息，不该因缓存不可用而报错。
    """
    t = db.get(ReviewTask, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    live: str | None = None
    try:
        live = get_redis().get(key_task_progress(task_id))
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不阻断查询
        logger.warning("读取任务进度缓存失败（退回数据库）: %s", exc)

    return TaskProgressOut(
        task_id=t.id, status=t.status, parsed_pages=t.parsed_pages,
        total_pages=t.total_pages, live_progress=live,
    )


@router.post("/{task_id}/retry", response_model=TaskOut, summary="重试（阻塞任务）")
def retry_task(
    task_id: int,
    db: Session = Depends(get_db),
    operator: str = Query(default="admin", description="操作人"),
    source_path: str | None = Query(
        default=None, description="本地文件路径；留空则从 MinIO 重新拉取原件"
    ),
) -> TaskOut:
    """重试受阻任务。

    **只允许 blocked → parsing**（状态机约束）。重试前先清理旧产物，
    避免新解析产生的条款与旧锚点并存（§6.2、§13.3）。
    """
    t = db.get(ReviewTask, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if t.status != TaskStatus.BLOCKED.value:
        raise HTTPException(
            status_code=409,
            detail=f"只有 blocked 任务可重试（当前 {t.status}）",
        )

    deleted = sm.cleanup_for_retry(db, t)
    sm.transition(db, t, TaskStatus.PARSING, operator=operator, detail="人工重试")
    db.add(_retry_event(t.id, operator, deleted))
    db.commit()
    logger.info("任务 #%s 重试，清理: %s", task_id, deleted)

    _schedule_pipeline(t.id, source_path)
    return _to_task_out(t)


def _retry_event(task_id: int, operator: str, deleted: dict[str, int]):
    from app.models import TaskEvent
    summary = "、".join(f"{k}={v}" for k, v in deleted.items()) or "无旧产物"
    return TaskEvent(
        task_id=task_id, event_type=TaskEventType.RETRY.value,
        from_status=TaskStatus.BLOCKED.value, to_status=TaskStatus.PARSING.value,
        operator=operator, detail=f"重试清理: {summary}",
    )


def _schedule_pipeline(task_id: int, source_path: str | None) -> None:
    """后台线程执行流水线。

    源文件优先用调用方给的本地路径（演示时直接指向 samples/）；
    没给则从 MinIO 拉原件到临时文件——这样不依赖本机是否存在该文件，
    审批系统同步来的合同走的就是这条路径。
    """
    def _run() -> None:
        tmp: Path | None = None
        try:
            from app.core.database import SessionLocal
            from app.services.llm import get_provider
            from app.workers.pipeline import Pipeline

            db2 = SessionLocal()
            try:
                t2 = db2.get(ReviewTask, task_id)
                if t2 is None:
                    return
                if source_path:
                    path = Path(source_path)
                else:
                    path, tmp = _fetch_original(db2, t2.contract_id)
                Pipeline(db2, t2, provider=get_provider()).run(path)
            finally:
                db2.close()
        except Exception:  # noqa: BLE001 - 后台线程不向请求抛异常
            logger.error("后台流水线 #%s 异常", task_id, exc_info=True)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    threading.Thread(target=_run, name=f"pipeline-{task_id}", daemon=True).start()


def _fetch_original(db: Session, contract_id: int) -> tuple[Path, Path]:
    """从 MinIO 拉原件到临时文件。返回 (路径, 需清理的临时路径)。"""
    contract = db.get(Contract, contract_id)
    if contract is None:
        raise RuntimeError(f"合同不存在: {contract_id}")
    ext = Path(contract.file_name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fh:
        tmp = Path(fh.name)
    resp = get_minio().get_object(settings.minio_bucket_contracts, contract.file_object_key)
    try:
        tmp.write_bytes(resp.read())
    finally:
        resp.close()
        resp.release_conn()
    logger.info("从 MinIO 拉取原件: %s -> %s", contract.file_object_key, tmp)
    return tmp, tmp


@router.post("/sync-todos", response_model=list[TaskOut], summary="同步审批系统待办")
def sync_todos(
    db: Session = Depends(get_db),
    status: str = Query(default="pending", description="拉取哪些状态的待办"),
    auto_review: bool = Query(default=True, description="拉取后立即启动审查"),
) -> list[TaskOut]:
    """从审批系统拉取待办，为每份附件建合同 + 任务。

    **幂等**：按 `file_hash` 去重，已存在的合同不重复建任务。
    这与"审批系统重复推送事件"的真实场景对应。
    """
    adapter = get_adapter()
    try:
        todos = adapter.list_todos(status)
    except ApprovalError as exc:
        raise HTTPException(status_code=502, detail=f"审批系统不可用: {exc}") from exc

    created: list[ReviewTask] = []
    for todo in todos:
        for att in todo.attachments:
            try:
                task = _sync_one(db, adapter, todo, att, auto_review=auto_review)
            except ApprovalError as exc:
                logger.error("同步待办 %s 的附件失败: %s", todo.approval_no, exc)
                continue
            if task is not None:
                created.append(task)

    return [_to_task_out(t) for t in created]


def _sync_one(db: Session, adapter, todo, att, *, auto_review: bool) -> ReviewTask | None:
    """同步单个附件。已存在（同 file_hash）则返回 None。"""
    import hashlib

    from app.models.enums import ContractSource, FileFormat

    downloaded = adapter.download_attachment(todo.approval_no, att.attachment_id)
    file_hash = hashlib.sha256(downloaded.content).hexdigest()

    existing = db.execute(
        select(Contract).where(
            Contract.file_hash == file_hash, Contract.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("待办 %s 的附件已存在（合同 #%s），跳过", todo.approval_no, existing.id)
        return None

    ext = Path(downloaded.file_name).suffix.lstrip(".").lower() or "bin"
    fmt = {
        "docx": FileFormat.DOCX.value, "pdf": FileFormat.PDF.value,
    }.get(ext, FileFormat.PDF.value)

    contract = Contract(
        title=todo.title, business_type=todo.business_type, file_format=fmt,
        file_object_key="pending", file_name=downloaded.file_name,
        file_size=downloaded.size, file_hash=file_hash,
        applicant=todo.applicant, applicant_dept=todo.applicant_dept,
        counterparty_name=todo.counterparty,
        source=ContractSource.APPROVAL_SYNC.value, external_id=todo.approval_no,
    )
    db.add(contract)
    db.flush()

    key = path_original(contract.id, ext)
    get_minio().put_object(
        settings.minio_bucket_contracts, key, io.BytesIO(downloaded.content),
        length=downloaded.size, content_type=downloaded.content_type,
    )
    contract.file_object_key = key

    task = ReviewTask(contract_id=contract.id, status=TaskStatus.PENDING.value)
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(
        "同步待办成功: %s -> 合同 #%s 任务 #%s",
        todo.approval_no, contract.id, task.id,
    )

    if auto_review:
        _schedule_pipeline(task.id, None)
    return task
