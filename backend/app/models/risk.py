"""风险域模型：risk_item / anchor / risk_evidence。

设计依据：docs/data-model.md §5.6 ~ §5.8。

`anchor` 用**多态关联**（owner_type + owner_id）服务多个 owner，
因此**没有数据库级外键**，孤儿行由 scripts/check_consistency.py 的 C1 检查兜底。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    AnchorLevel,
    AnchorSource,
    EvidenceType,
    MergedBy,
    RiskCategory,
    RiskLevel,
)
from app.models.types import (
    DATETIME_MS,
    INT_UNSIGNED,
    PK_TYPE,
    bool_col,
    created_at_col,
    enum_col,
    fk_col,
    pk_col,
    updated_at_col,
)

if TYPE_CHECKING:
    from app.models.core import Clause, Contract


class RiskItem(Base):
    """风险项：审查的核心产出，是报告"风险清单明细"的数据源。

    不变量（见 §5.6）：
    - I1 `is_global = 1` ⟹ `clause_id IS NULL`
    - I2 `unanchored = 0` ⟹ 至少存在一条 anchor
    - I3 `merged_by = 'both'` ⟹ 同时存在 rule 与 llm 两类依据
    - I4 `suggestion_edited` 非空 ⟹ `adopted = 1`
    """

    __tablename__ = "risk_item"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")
    clause_id: Mapped[int | None] = fk_col(
        "clause.id", nullable=True, ondelete="SET NULL", comment="全局性问题为 NULL"
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="风险项名称")
    risk_level: Mapped[str] = enum_col(RiskLevel, comment="风险等级")
    category: Mapped[str] = enum_col(RiskCategory, comment="风险分类")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="风险成因（面向用户）")
    legal_basis: Mapped[str | None] = mapped_column(Text, comment="法律合规依据")
    suggestion: Mapped[str | None] = mapped_column(Text, comment="AI 推荐修改条款")
    suggestion_edited: Mapped[str | None] = mapped_column(Text, comment="法务编辑版，非空时优先展示")

    adopted: Mapped[int] = bool_col(comment="法务是否采纳建议")
    merged_by: Mapped[str] = enum_col(MergedBy, comment="定级来源，实现双来源留痕")
    is_global: Mapped[int] = bool_col(comment="是否全局性问题（非条款级）")
    unanchored: Mapped[int] = bool_col(comment="引用无法定位（防幻觉闸门标记）")

    seq: Mapped[int] = mapped_column(nullable=False, comment="展示顺序（按等级+页码固化）")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    contract: Mapped["Contract"] = relationship(back_populates="risk_items")
    clause: Mapped["Clause | None"] = relationship(back_populates="risk_items")
    evidences: Mapped[list["RiskEvidence"]] = relationship(
        back_populates="risk_item", cascade="all, delete-orphan", passive_deletes=True
    )
    # 注意：anchor 走多态关联，**故意不定义 relationship**。
    # 多态列没有数据库外键，ORM 无法推断 join 条件，硬写 primaryjoin 会
    # 制造"看似有外键"的假象；统一通过 app.models.anchor_repo 的查询助手访问。

    __table_args__ = (
        Index("idx_contract_level", "contract_id", "risk_level"),
        Index("idx_contract_seq", "contract_id", "seq"),
        Index("idx_clause_id", "clause_id"),
        Index("idx_unanchored", "unanchored"),
    )

    def __repr__(self) -> str:
        return f"<RiskItem id={self.id} level={self.risk_level} title={self.title!r}>"


class Anchor(Base):
    """锚点：统一的位置引用，多态关联到 risk_item / contract_metadata。

    不变量（见 §5.7）：
    - `bbox_x0 < bbox_x1` 且 `bbox_y0 < bbox_y1`
    - `source = 'native_text'` ⟹ `char_start` / `char_end` 非空
    - `anchor_level = 'paragraph'` ⟹ `char_start` / `char_end` 为 NULL
    - `anchor_level = 'none'` 的行**不应存在**（改用 risk_item.unanchored）
    """

    __tablename__ = "anchor"

    id: Mapped[int] = pk_col()

    # 多态关联：无数据库外键，写入只经 ORM 关系，孤儿行由 C1 检查发现
    owner_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="risk_item / contract_metadata"
    )
    owner_id: Mapped[int] = mapped_column(PK_TYPE, nullable=False, comment="多态外键")
    seq: Mapped[int] = mapped_column(nullable=False, default=0, comment="同一 owner 内多片段顺序")

    page_no: Mapped[int] = mapped_column(nullable=False, comment="页码，从 1 开始")
    bbox_x0: Mapped[float] = mapped_column(nullable=False, comment="左上 X（PDF point）")
    bbox_y0: Mapped[float] = mapped_column(nullable=False)
    bbox_x1: Mapped[float] = mapped_column(nullable=False, comment="右下 X")
    bbox_y1: Mapped[float] = mapped_column(nullable=False)

    char_start: Mapped[int | None] = mapped_column(INT_UNSIGNED, comment="全文字符起始；扫描件为 NULL")
    char_end: Mapped[int | None] = mapped_column(INT_UNSIGNED, comment="字符结束（不含）")
    quote_text: Mapped[str | None] = mapped_column(String(512), comment="命中的原文片段，用于校验")

    source: Mapped[str] = enum_col(AnchorSource, comment="坐标来源")
    anchor_level: Mapped[str] = enum_col(AnchorLevel, comment="对齐层级（exact/fuzzy/paragraph）")
    confidence: Mapped[float | None] = mapped_column(comment="OCR 置信度")
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        # 主查询路径：按 owner 取全部锚点
        Index("idx_owner", "owner_type", "owner_id", "seq"),
        Index("idx_page", "page_no"),
    )


class RiskEvidence(Base):
    """风险依据：支撑风险判定的依据链，实现"双来源留痕"与防幻觉。

    不变量（见 §5.8）：
    - `evidence_type = 'rule'` ⟹ `rule_id` 非空
    - `evidence_type = 'llm'` ⟹ `raw_snippet` 非空（保留原始输出以便审计）
    """

    __tablename__ = "risk_evidence"

    id: Mapped[int] = pk_col()
    risk_item_id: Mapped[int] = fk_col("risk_item.id")

    evidence_type: Mapped[str] = enum_col(EvidenceType, comment="依据来源")
    rule_id: Mapped[int | None] = fk_col(
        "rule.id", nullable=True, ondelete="SET NULL", comment="evidence_type=rule 时的来源规则"
    )
    title: Mapped[str | None] = mapped_column(String(255), comment="依据标题，如《民法典》第五百八十五条")
    detail: Mapped[str] = mapped_column(Text, nullable=False, comment="依据详情")
    raw_snippet: Mapped[str | None] = mapped_column(Text, comment="LLM 原始输出片段（审计用）")
    need_review: Mapped[int] = bool_col(comment="LLM 编造法条时置 1")
    created_at: Mapped[datetime] = created_at_col()

    risk_item: Mapped["RiskItem"] = relationship(back_populates="evidences")

    __table_args__ = (Index("idx_risk_item", "risk_item_id", "evidence_type"),)
