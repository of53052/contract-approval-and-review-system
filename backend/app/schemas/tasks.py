"""任务与回写相关响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TaskOut(BaseModel):
    """审查任务。"""

    id: int
    contract_id: int
    status: str
    writeback_status: str
    blocked_reason: str | None = None
    blocked_detail: str | None = None
    overall_risk: str | None = None
    conclusion: str | None = None
    summary: str | None = None
    total_pages: int | None = None
    parsed_pages: int = 0
    high_risk_count: int = 0
    medium_risk_count: int = 0
    low_risk_count: int = 0
    parse_duration_ms: int | None = None
    review_duration_ms: int | None = None
    started_at: datetime | None = None
    blocked_at: datetime | None = None
    completed_at: datetime | None = None
    version: int
    created_at: datetime


class TaskProgressOut(BaseModel):
    """任务进度（前端轮询）。"""

    task_id: int
    status: str
    parsed_pages: int = 0
    total_pages: int | None = None
    #: Redis 里的实时进度字符串，形如 "3/12"；缺失时为 None
    live_progress: str | None = None


class UploadResultOut(BaseModel):
    """上传合同的结果。"""

    contract_id: int
    task_id: int
    file_name: str
    file_size: int
    file_hash: str
    #: 命中去重时返回已存在的合同，不新建任务
    deduplicated: bool = False
    detail: str | None = None


class WritebackIn(BaseModel):
    """触发回写。"""

    author: str = Field(default="合同审查系统", description="回写人")
    force: bool = Field(default=False, description="跳过幂等检查，强制重新回写")


class WritebackOut(BaseModel):
    """回写结果。"""

    contract_id: int
    status: str
    log_id: int
    comment_id: str | None = None
    deduplicated: bool = False
    http_status: int | None = None
    duration_ms: int = 0
    error_detail: str | None = None


class WritebackStatusOut(BaseModel):
    """回写状态（前端轮询）。"""

    writeback_status: str
    latest_log_id: int | None = None
    latest_status: str | None = None
    error_detail: str | None = None
    created_at: str | None = None


class ExportOut(BaseModel):
    """报告导出结果。"""

    record_id: int
    object_key: str
    file_size: int
    format: str
    #: 下载地址（后端代理 MinIO，避免前端直连对象存储）
    download_url: str


class ReportPreviewOut(BaseModel):
    """报告预览（Markdown 原文）。"""

    contract_id: int
    markdown: str
    char_count: int
