"""任务状态机。

设计依据：docs/architecture.md §7.1、docs/data-model.md §6.1/§6.2。

状态流转：
    pending → parsing → reviewing → completed
    parsing → blocked（可重试回 parsing）

**并发控制**：`review_task.version` 乐观锁，防止同一任务被并发重复执行。
**重试清理**：重试前必须删除旧产物（clause / metadata / risk_item），
否则新解析产生的条款与旧锚点会同时存在，锚点指向已失效的条款。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    Anchor,
    Clause,
    ContractMetadata,
    LlmCallLog,
    ParseResult,
    ReviewTask,
    RiskEvidence,
    RiskItem,
    TaskEvent,
)
from app.models.enums import TaskEventType, TaskStatus

logger = logging.getLogger(__name__)

#: 允许的状态流转。不在表中的流转一律拒绝——宁可报错也不让状态机失控。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PARSING, TaskStatus.BLOCKED}),
    TaskStatus.PARSING: frozenset({TaskStatus.REVIEWING, TaskStatus.BLOCKED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.COMPLETED, TaskStatus.BLOCKED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PARSING}),
    TaskStatus.COMPLETED: frozenset(),  # 终态
}


class InvalidTransition(Exception):
    """非法状态流转。"""


def transition(
    db: Session,
    task: ReviewTask,
    to_status: TaskStatus,
    *,
    operator: str = "system",
    detail: str | None = None,
    expected_version: int | None = None,
) -> ReviewTask:
    """执行状态流转，并写 `task_event` 审计。

    `expected_version` 非空时启用乐观锁：版本不匹配则抛 `InvalidTransition`，
    避免并发执行把同一个任务跑两遍。
    """
    from_status = TaskStatus(task.status)
    if to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise InvalidTransition(
            f"不允许的状态流转: {from_status.value} -> {to_status.value}"
        )

    if expected_version is not None and task.version != expected_version:
        raise InvalidTransition(
            f"乐观锁冲突: 期望 version={expected_version}，实际 {task.version}"
        )

    task.status = to_status.value
    task.version = task.version + 1

    # 时间戳：进入各阶段时记录，供"耗时"统计与审计
    now = datetime.now()
    if to_status is TaskStatus.PARSING and task.started_at is None:
        task.started_at = now
    elif to_status is TaskStatus.BLOCKED:
        task.blocked_at = now
    elif to_status is TaskStatus.COMPLETED:
        task.completed_at = now

    db.add(TaskEvent(
        task_id=task.id,
        event_type=TaskEventType.STATUS_CHANGE.value,
        from_status=from_status.value,
        to_status=to_status.value,
        operator=operator,
        detail=detail,
    ))
    db.flush()
    logger.info(
        "任务 %s 状态流转: %s -> %s%s",
        task.id, from_status.value, to_status.value,
        f"（{detail}）" if detail else "",
    )
    return task


def block(
    db: Session,
    task: ReviewTask,
    reason: str,
    detail: str,
) -> ReviewTask:
    """把任务置为 blocked，写入原因与详情（不变量 I1）。"""
    task.blocked_reason = reason
    task.blocked_detail = detail[:512]
    result = transition(db, task, TaskStatus.BLOCKED, detail=f"{reason}: {detail}")
    db.add(TaskEvent(
        task_id=task.id,
        event_type=TaskEventType.BLOCKED.value,
        from_status=None,
        to_status=TaskStatus.BLOCKED.value,
        operator="system",
        detail=f"{reason}: {detail}"[:1024],
    ))
    return result


def record_progress(db: Session, task: ReviewTask, parsed_pages: int, total_pages: int) -> None:
    """上报解析进度。

    不变量 I3：`parsed_pages ≤ total_pages`。
    """
    task.parsed_pages = min(parsed_pages, total_pages)
    task.total_pages = total_pages
    db.add(TaskEvent(
        task_id=task.id,
        event_type=TaskEventType.PROGRESS.value,
        detail=f"已解析 {task.parsed_pages}/{total_pages} 页",
    ))


def cleanup_for_retry(db: Session, task: ReviewTask) -> dict[str, int]:
    """重试前清理旧产物（docs/data-model.md §6.2）。

    **保留**：`parse_result`（追加 attempt）、`annotation`（人工批注）、`task_event`（审计）
    **删除**：`clause`、`contract_metadata`、`risk_item` + `anchor` + `risk_evidence`

    删除顺序遵循外键依赖：先删引用方（anchor / evidence），再删被引用方。
    `anchor` 是多态关联无外键，需按 owner 显式清理。
    """
    contract_id = task.contract_id
    deleted: dict[str, int] = {}

    # risk_item 的 id 集合，用于清理多态 anchor
    risk_ids = list(db.execute(
        select(RiskItem.id).where(RiskItem.contract_id == contract_id)
    ).scalars())

    if risk_ids:
        # anchor 无外键，必须显式按 owner 删（否则会留下孤儿锚点）
        n = db.execute(
            delete(Anchor).where(
                Anchor.owner_type == "risk_item", Anchor.owner_id.in_(risk_ids)
            )
        ).rowcount
        deleted["anchor"] = n

        n = db.execute(
            delete(RiskEvidence).where(RiskEvidence.risk_item_id.in_(risk_ids))
        ).rowcount
        deleted["risk_evidence"] = n

    # 元数据的锚点（同样是多态）
    meta_ids = list(db.execute(
        select(ContractMetadata.id).where(ContractMetadata.contract_id == contract_id)
    ).scalars())
    if meta_ids:
        n = db.execute(
            delete(Anchor).where(
                Anchor.owner_type == "contract_metadata", Anchor.owner_id.in_(meta_ids)
            )
        ).rowcount
        deleted["anchor"] = deleted.get("anchor", 0) + n

    n = db.execute(delete(RiskItem).where(RiskItem.contract_id == contract_id)).rowcount
    deleted["risk_item"] = n

    n = db.execute(
        delete(ContractMetadata).where(ContractMetadata.contract_id == contract_id)
    ).rowcount
    deleted["contract_metadata"] = n

    # clause 被 risk_item.clause_id 引用，需在 risk_item 之后删
    n = db.execute(delete(Clause).where(Clause.contract_id == contract_id)).rowcount
    deleted["clause"] = n

    # 重置任务字段（保留 parse_result 历史，attempt 由执行器递增）
    task.blocked_reason = None
    task.blocked_detail = None
    task.blocked_at = None
    task.overall_risk = None
    task.conclusion = None
    task.summary = None
    task.parsed_pages = 0
    task.high_risk_count = 0
    task.medium_risk_count = 0
    task.low_risk_count = 0
    task.completed_at = None

    logger.info("任务 %s 重试清理完成: %s", task.id, deleted)
    return deleted


def next_attempt(db: Session, contract_id: int) -> int:
    """计算下一次解析的 attempt 序号（`parse_result` 保留历史，递增）。"""
    latest = db.execute(
        select(ParseResult.attempt)
        .where(ParseResult.contract_id == contract_id)
        .order_by(ParseResult.attempt.desc())
        .limit(1)
    ).scalar()
    return (latest or 0) + 1


def log_llm_call(
    db: Session,
    *,
    task_id: int | None,
    provider: str,
    model: str | None,
    purpose: str,
    status: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    json_retry_count: int = 0,
    duration_ms: int | None = None,
    error_detail: str | None = None,
) -> None:
    """记录一次 LLM 调用。

    **不在循环体内调用**（AGENTS.md 日志策略）：每次调用完成后落一条，
    而不是每次 token 或每个条款都写。
    """
    db.add(LlmCallLog(
        task_id=task_id,
        provider=provider,
        model=model,
        purpose=purpose,
        status=status,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        json_retry_count=json_retry_count,
        duration_ms=duration_ms,
        error_detail=error_detail,
    ))


def refresh_version(db: Session, task_id: int) -> int:
    """从数据库重读 version（乐观锁校验用）。"""
    return db.execute(
        select(ReviewTask.version).where(ReviewTask.id == task_id)
    ).scalar() or 0


def mark_stale_tasks(db: Session, *, timeout_seconds: int) -> int:
    """把卡在 parsing/reviewing 超时的任务标记为 blocked。

    用途：进程崩溃后重启，靠这个函数恢复状态（§13.1 "重启恢复靠 parsing 状态"）。
    """
    rows = db.execute(
        select(ReviewTask.id, ReviewTask.status).where(
            ReviewTask.status.in_([TaskStatus.PARSING.value, TaskStatus.REVIEWING.value]),
            ReviewTask.updated_at < text(f"NOW(3) - INTERVAL {int(timeout_seconds)} SECOND"),
        )
    ).fetchall()
    for task_id, _ in rows:
        db.execute(
            update(ReviewTask).where(ReviewTask.id == task_id).values(
                status=TaskStatus.BLOCKED.value,
                blocked_reason="timeout",
                blocked_detail="进程重启或执行超时，任务被标记为受阻，可重试",
                blocked_at=datetime.now(),
                version=ReviewTask.version + 1,
            )
        )
    if rows:
        logger.warning("标记 %s 个超时任务为 blocked: %s", len(rows), [r[0] for r in rows])
    return len(rows)
