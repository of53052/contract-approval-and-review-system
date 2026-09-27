"""mock 审批服务的 HTTP 契约。

⚠️ 本服务扮演**外部系统**，与 backend **不共享代码**（architecture.md §17）。
这里的模型只描述对外暴露的 JSON 结构，后端有自己的适配层模型。

字段命名用外部系统的视角（`approval_no` 而非 `external_id`），
刻意与后端模型不同名——这样"适配层做了映射"这件事是显式的，
将来换成真实审批系统时，改动面被限制在适配层内。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Attachment(BaseModel):
    """审批单附件。"""

    attachment_id: str = Field(description="附件 ID")
    file_name: str = Field(description="原始文件名")
    file_size: int = Field(description="字节数")
    content_type: str = Field(description="MIME 类型")
    download_url: str = Field(description="下载地址（相对路径）")


class TodoItem(BaseModel):
    """待办审批单。"""

    approval_no: str = Field(description="审批单号")
    title: str = Field(description="合同名称")
    applicant: str = Field(description="申请人")
    applicant_dept: str = Field(description="送审部门")
    business_type: Literal["purchase", "sales", "service", "labor"] = Field(
        description="业务类型"
    )
    counterparty: str = Field(description="相对方名称")
    status: Literal["pending", "approved", "rejected"] = Field(description="审批状态")
    submitted_at: datetime = Field(description="提交时间")
    attachments: list[Attachment] = Field(default_factory=list)


class CommentIn(BaseModel):
    """写入评论的请求体。"""

    author: str = Field(description="评论人")
    content: str = Field(description="评论内容（Markdown）")
    #: 幂等键：同一审批单重复提交相同 key 只落一条，供回写幂等使用
    idempotency_key: str | None = Field(default=None, description="幂等键")


class Comment(BaseModel):
    """评论记录。"""

    comment_id: str
    approval_no: str
    author: str
    content: str
    idempotency_key: str | None = None
    created_at: datetime


class CommentOut(BaseModel):
    """写入评论的响应。"""

    comment_id: str
    approval_no: str
    created_at: datetime
    #: 是否命中幂等（重复提交返回 true，未新建）
    deduplicated: bool = False


class EventPushOut(BaseModel):
    """事件推送结果。"""

    approval_no: str
    target_url: str
    http_status: int | None = None
    delivered: bool
    detail: str | None = None
