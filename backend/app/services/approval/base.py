"""审批系统适配层抽象。

设计依据：docs/architecture.md §3（mock 审批扮演外部系统）、§17（目录划分原则）。

**唯一契约**：业务层只依赖 `ApprovalSystemAdapter` 的四个方法，
不感知底层是 HTTP、本地 mock，还是将来真实审批系统的 SDK。
换实现只改 `get_adapter()` 的装配逻辑，业务代码零改动。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.approval.types import (
    DownloadedFile,
    RemoteTodo,
    WriteCommentResult,
)


class ApprovalError(Exception):
    """审批系统调用失败。

    **不吞异常**：调用方（回写服务）需要区分"对方返回业务错误"
    与"网络不通"，前者记 failed 可重试，后者记 failed 并保留详情。
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class ApprovalNotFound(ApprovalError):
    """审批单或附件不存在（404）。**不可重试**，重试还是 404。"""


@runtime_checkable
class ApprovalSystemAdapter(Protocol):
    """审批系统适配器接口。

    四个能力对应架构文档 §3 的 M1~M4。
    """

    @property
    def name(self) -> str:
        """实现名，用于日志与 `writeback_log` 溯源。"""
        ...

    def health(self) -> tuple[bool, str]:
        """连通性自检。返回 (是否可用, 详情)。"""
        ...

    def list_todos(self, status: str | None = None) -> list[RemoteTodo]:
        """拉取待办审批单列表。"""
        ...

    def get_todo(self, approval_no: str) -> RemoteTodo:
        """取单个审批单。不存在时抛 `ApprovalNotFound`。"""
        ...

    def download_attachment(
        self, approval_no: str, attachment_id: str
    ) -> DownloadedFile:
        """下载附件字节流。不存在时抛 `ApprovalNotFound`。"""
        ...

    def write_comment(
        self,
        approval_no: str,
        author: str,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> WriteCommentResult:
        """写入审查意见评论。"""
        ...
