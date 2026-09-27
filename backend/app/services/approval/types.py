"""审批系统对接的领域类型。

**为什么要有这一层**：后端不应该到处传外部系统的 JSON 字典。
把"外部系统长什么样"限制在适配层内，业务层只认这里的类型，
将来换真实审批系统时改动面可控（architecture.md §17 的目录划分原则）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RemoteAttachment:
    """外部系统的附件描述。"""

    attachment_id: str
    file_name: str
    file_size: int
    content_type: str
    download_url: str


@dataclass
class RemoteTodo:
    """外部系统的待办审批单。

    字段刻意用外部系统的语义命名（`approval_no` 而非 `external_id`），
    映射到本项目模型的动作在 `service.py` 里显式完成。
    """

    approval_no: str
    title: str
    applicant: str
    applicant_dept: str
    business_type: str
    counterparty: str
    status: str
    submitted_at: datetime
    attachments: list[RemoteAttachment] = field(default_factory=list)


@dataclass
class DownloadedFile:
    """下载到的附件。`content` 为原始字节流。"""

    file_name: str
    content: bytes
    content_type: str

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class WriteCommentResult:
    """写入评论的结果。"""

    comment_id: str
    created_at: datetime
    #: 是否命中服务端幂等（重复提交返回 true）
    deduplicated: bool = False
    #: HTTP 状态码，便于落 `writeback_log.http_status`
    http_status: int | None = None
