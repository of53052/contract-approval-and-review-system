"""风险相关响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AnchorOut(BaseModel):
    """锚点。前端据此跳页 + 画框 + 文本层高亮。"""

    id: int
    page_no: int
    bbox_x0: float
    bbox_y0: float
    bbox_x1: float
    bbox_y1: float
    char_start: int | None = None
    char_end: int | None = None
    quote_text: str | None = None
    source: str
    anchor_level: str
    confidence: float | None = None


class EvidenceOut(BaseModel):
    """依据链的一条。`need_review` 是防幻觉闸门标记。"""

    id: int
    evidence_type: str
    title: str | None = None
    detail: str
    raw_snippet: str | None = None
    rule_id: int | None = None
    need_review: bool


class RiskItemOut(BaseModel):
    """风险项（工作台右栏卡片）。"""

    id: int
    title: str
    risk_level: str
    category: str
    reason: str
    legal_basis: str | None = None
    suggestion: str | None = None
    suggestion_edited: str | None = None
    adopted: bool
    merged_by: str
    is_global: bool
    unanchored: bool
    seq: int
    clause_id: int | None = None
    #: 命中条款的原文。供前端"条款差异对比"（PRD 2.4.5）展示原文侧，
    #: 避免前端再拉一次条款列表并按 id 反查。
    clause_content: str | None = None

    anchors: list[AnchorOut] = Field(default_factory=list)
    evidences: list[EvidenceOut] = Field(default_factory=list)


class RiskUpdateIn(BaseModel):
    """更新风险项（法务编辑建议 / 采纳）。"""

    suggestion_edited: str | None = Field(
        default=None, description="法务编辑版建议；传空串表示清空"
    )
    adopted: bool | None = Field(default=None, description="是否采纳")


class AnnotationIn(BaseModel):
    """新增法务批注。"""

    author: str = Field(description="批注人")
    content: str = Field(description="批注内容")
    author_role: str | None = Field(default=None, description="角色，如 法务审查人")
    risk_item_id: int | None = Field(default=None, description="关联风险项；整体意见留空")


class AnnotationOut(BaseModel):
    """法务批注。"""

    id: int
    contract_id: int
    risk_item_id: int | None = None
    author: str
    author_role: str | None = None
    content: str
    created_at: datetime
