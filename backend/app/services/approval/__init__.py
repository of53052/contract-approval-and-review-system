"""审批系统适配层。

**装配点**：`get_adapter()` 是唯一决定"用哪个实现"的地方。
业务层统一用 `get_adapter()` 拿实例，不直接实例化具体类——
这样"换真实审批系统只改一处"是可验证的。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.services.approval.base import (
    ApprovalError,
    ApprovalNotFound,
    ApprovalSystemAdapter,
)
from app.services.approval.http_adapter import HttpApprovalAdapter
from app.services.approval.types import (
    DownloadedFile,
    RemoteAttachment,
    RemoteTodo,
    WriteCommentResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalError",
    "ApprovalNotFound",
    "ApprovalSystemAdapter",
    "HttpApprovalAdapter",
    "DownloadedFile",
    "RemoteAttachment",
    "RemoteTodo",
    "WriteCommentResult",
    "get_adapter",
]

_adapter: ApprovalSystemAdapter | None = None


def get_adapter(*, force_new: bool = False) -> ApprovalSystemAdapter:
    """获取审批系统适配器（进程内单例）。

    单例是为了复用 HTTP 连接池。测试若需独立实例，传 `force_new=True`。
    """
    global _adapter
    if _adapter is None or force_new:
        _adapter = HttpApprovalAdapter()
        logger.info("审批系统适配器: %s -> %s", _adapter.name, _adapter.base_url)
    return _adapter
