"""合同相关响应模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.risks import AnchorOut


class ContractListItem(BaseModel):
    """大盘页列表项。

    字段覆盖 PRD 2.4.3 要求：合同名称、申请人、业务类型、合同金额、
    审查状态、综合风险等级、创建时间。冗余字段来自 `review_task`，
    避免列表页 N+1 聚合（原则 P4）。
    """

    id: int
    title: str
    contract_no: str | None = None
    business_type: str
    amount: Decimal | None = None
    currency: str | None = None
    applicant: str | None = None
    applicant_dept: str | None = None
    counterparty_name: str | None = None

    # 来自 review_task 的冗余字段
    task_id: int | None = None
    status: str = "pending"
    overall_risk: str | None = None
    conclusion: str | None = None
    high_risk_count: int = 0
    medium_risk_count: int = 0
    low_risk_count: int = 0
    writeback_status: str = "not_written"
    blocked_reason: str | None = None
    parsed_pages: int = 0
    total_pages: int | None = None

    created_at: datetime


class ContractDetail(ContractListItem):
    """工作台用的合同详情（含文件信息）。"""

    file_format: str
    file_name: str
    file_size: int
    pdf_object_key: str | None = None
    source: str
    external_id: str | None = None
    summary: str | None = None


class ClauseOut(BaseModel):
    """条款。`char_start`/`char_end` 供前端文本层高亮。"""

    id: int
    clause_type: str
    clause_no: str | None = None
    title: str | None = None
    content: str
    page_no: int
    page_end: int | None = None
    para_index: int
    char_start: int | None = None
    char_end: int | None = None
    bbox_x0: float | None = None
    bbox_y0: float | None = None
    bbox_x1: float | None = None
    bbox_y1: float | None = None
    seq: int
    source: str


class MetadataOut(BaseModel):
    """元数据项。`need_review` 供前端高亮待核对。

    `anchors` 是该元数据在原文中的位置，供工作台"高亮标记提取的
    元数据字段"（PRD 2.4.3）。为空表示该字段未锚定到原文
    （如从表格或 OCR 提取、坐标不可靠），前端只做文字提示不高亮。
    """

    id: int
    meta_key: str
    meta_value: str | None = None
    value_normalized: str | None = None
    value_type: str
    confidence: float
    need_review: bool
    anchors: list[AnchorOut] = Field(default_factory=list)


class TaskEventOut(BaseModel):
    """任务事件（审计轨迹）。"""

    id: int
    event_type: str
    from_status: str | None = None
    to_status: str | None = None
    operator: str | None = None
    detail: str | None = None
    created_at: datetime
