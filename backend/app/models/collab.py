"""协同与审计域模型：task_event / annotation / writeback_log / export_record / llm_call_log。

设计依据：docs/data-model.md §5.9 ~ §5.13。

这一域的共同特征：**只追加、不修改**（除 writeback_log 的状态流转外），
是"可溯源"要求的落地载体。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    TaskEventType,
    TaskStatus,
    WritebackStatus,
)
from app.models.types import (
    BIGINT_UNSIGNED,
    DATETIME_MS,
    INT_UNSIGNED,
    MEDIUM_TEXT,
    bool_col,
    created_at_col,
    enum_col,
    fk_col,
    pk_col,
    updated_at_col,
)

if TYPE_CHECKING:
    from app.models.core import Contract, ReviewTask


class TaskEvent(Base):
    """任务事件：状态流转审计。回答"这个任务为什么变成了 blocked"。"""

    __tablename__ = "task_event"

    id: Mapped[int] = pk_col()
    task_id: Mapped[int] = fk_col("review_task.id")

    event_type: Mapped[str] = enum_col(TaskEventType, comment="事件类型")
    from_status: Mapped[str | None] = enum_col(
        TaskStatus, nullable=True, comment="变更前状态"
    )
    to_status: Mapped[str | None] = enum_col(
        TaskStatus, nullable=True, comment="变更后状态"
    )
    operator: Mapped[str | None] = mapped_column(String(64), comment="操作人；系统操作为 system")
    detail: Mapped[str | None] = mapped_column(String(1024), comment="事件详情")
    created_at: Mapped[datetime] = created_at_col()

    task: Mapped["ReviewTask"] = relationship(back_populates="events")

    __table_args__ = (Index("idx_task_created", "task_id", "created_at"),)


class Annotation(Base):
    """法务批注：人工意见，是回写内容的一部分。重试解析时**保留**（§6.2）。"""

    __tablename__ = "annotation"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")
    risk_item_id: Mapped[int | None] = fk_col(
        "risk_item.id", nullable=True, ondelete="SET NULL", comment="整体意见为 NULL"
    )

    author: Mapped[str] = mapped_column(String(64), nullable=False, comment="批注人")
    author_role: Mapped[str | None] = mapped_column(String(32), comment="角色，如 法务审查人")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="批注内容")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        Index("idx_contract_created", "contract_id", "created_at"),
        Index("idx_risk_item", "risk_item_id"),
    )


class WritebackLog(Base):
    """回写记录：审计与幂等保障。

    D10：**发起回写前先落 `writing` 记录**，防止进程崩溃导致状态丢失。
    幂等：同一合同至多一条 `status='success'`（应用层保证，C6 检查兜底）。
    """

    __tablename__ = "writeback_log"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")

    status: Mapped[str] = enum_col(WritebackStatus, comment="回写状态")
    payload: Mapped[str] = mapped_column(MEDIUM_TEXT, nullable=False, comment="发送的 Markdown 文本")
    response: Mapped[str | None] = mapped_column(Text, comment="审批系统返回原文")
    http_status: Mapped[int | None] = mapped_column(comment="HTTP 状态码")
    retry_count: Mapped[int] = mapped_column(
        INT_UNSIGNED, nullable=False, default=0, server_default="0"
    )
    error_detail: Mapped[str | None] = mapped_column(Text, comment="失败详情")
    duration_ms: Mapped[int | None] = mapped_column(INT_UNSIGNED)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        Index("idx_contract_created", "contract_id", "created_at"),
        Index("idx_status", "status"),
    )


class ExportRecord(Base):
    """导出记录：报告产物在 MinIO 中的引用。"""

    __tablename__ = "export_record"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")

    format: Mapped[str] = mapped_column(String(16), nullable=False, comment="markdown / pdf")
    file_object_key: Mapped[str] = mapped_column(String(512), nullable=False, comment="MinIO object key")
    file_size: Mapped[int | None] = mapped_column(BIGINT_UNSIGNED, comment="字节数")
    created_by: Mapped[str | None] = mapped_column(String(64), comment="导出人")
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (Index("idx_contract_created", "contract_id", "created_at"),)


class LlmCallLog(Base):
    """LLM 调用日志：审计与成本观测。

    约定：**不在循环体内写日志**，仅在每次调用完成后落一条（AGENTS.md 日志策略）。
    """

    __tablename__ = "llm_call_log"

    id: Mapped[int] = pk_col()
    task_id: Mapped[int | None] = fk_col(
        "review_task.id", nullable=True, ondelete="SET NULL"
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False, comment="mock / openai_compat")
    model: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="clause_review / suggestion / summary"
    )
    # success / failed / invalid_json —— invalid_json 单独成值才能度量提示词约束是否有效
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column()
    completion_tokens: Mapped[int | None] = mapped_column()
    json_retry_count: Mapped[int] = mapped_column(
        INT_UNSIGNED, nullable=False, default=0, server_default="0", comment="JSON 解析重试次数"
    )
    duration_ms: Mapped[int | None] = mapped_column(INT_UNSIGNED)
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        Index("idx_task_id", "task_id"),
        Index("idx_status", "status"),
    )
