"""配置域模型：rule_template / rule / rule_condition / standard_clause / subject_blacklist。

设计依据：docs/data-model.md §5.14 ~ §5.18。

这一域是**知识来源**，不随合同变化。`standard_clause` 同时充当防幻觉的
"法条白名单"：LLM 输出的法条若无法在此匹配，`risk_evidence.need_review` 置 1。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    BusinessType,
    ClauseType,
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
    ValueType,
)
from app.models.types import (
    JSON_TYPE,
    MEDIUM_TEXT,
    bool_col,
    created_at_col,
    enum_col,
    fk_col,
    pk_col,
    updated_at_col,
)


class RuleTemplate(Base):
    """规则模板：按合同类型组织的审查清单。每种合同类型一个模板。"""

    __tablename__ = "rule_template"

    id: Mapped[int] = pk_col()
    contract_type: Mapped[str] = enum_col(BusinessType, comment="适用的业务类型")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模板名称")
    description: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[int] = bool_col(default=1, comment="是否启用")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    rules: Mapped[list["Rule"]] = relationship(
        back_populates="template", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("uk_contract_type", "contract_type", unique=True),)


class Rule(Base):
    """审查规则：单条规则，含类型、等级、结果模板。

    `config` 用原生 JSON 列（如 `{"threshold": 0.20}`），这是选 MySQL
    而非 SQLite 的决定性因素之一（见 §9.1）。
    """

    __tablename__ = "rule"

    id: Mapped[int] = pk_col()
    template_id: Mapped[int] = fk_col("rule_template.id")

    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="规则编码，如 LIABILITY_UNEQUAL")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="规则名称")
    category: Mapped[str] = enum_col(RiskCategory, comment="风险分类")
    risk_level: Mapped[str] = enum_col(RiskLevel, comment="命中后的风险等级")
    rule_type: Mapped[str] = enum_col(RuleType, comment="匹配方式")
    config: Mapped[dict | None] = mapped_column(JSON_TYPE, comment="类型相关参数")
    result_template: Mapped[str | None] = mapped_column(Text, comment="结论模板，支持 {占位符}")
    suggestion_template: Mapped[str | None] = mapped_column(Text, comment="推荐修改条款模板")
    enabled: Mapped[int] = bool_col(default=1)
    seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0", comment="执行顺序")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    template: Mapped["RuleTemplate"] = relationship(back_populates="rules")
    conditions: Mapped[list["RuleCondition"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("uk_template_code", "template_id", "code", unique=True),
        Index("idx_enabled", "enabled"),
    )


class RuleCondition(Base):
    """规则触发条件。一条规则可有多个条件，**条件间为 AND**。

    OR 语义通过多条规则表达，保持实现简单（见 §5.16）。
    """

    __tablename__ = "rule_condition"

    id: Mapped[int] = pk_col()
    rule_id: Mapped[int] = fk_col("rule.id")

    seq: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0", comment="条件顺序")
    field: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="作用字段，如 clause.content / metadata.amount"
    )
    operator: Mapped[str] = enum_col(RuleOperator, comment="比较运算符")
    value: Mapped[str | None] = mapped_column(Text, comment="比较值；exists/not_exists 时为空")
    value_type: Mapped[str] = enum_col(
        ValueType, default=ValueType.STRING, comment="值类型标签"
    )

    rule: Mapped["Rule"] = relationship(back_populates="conditions")

    __table_args__ = (Index("idx_rule_seq", "rule_id", "seq"),)


class StandardClause(Base):
    """标准示范条款库：条款推荐的知识来源，同时是防幻觉的"法条白名单"。"""

    __tablename__ = "standard_clause"

    id: Mapped[int] = pk_col()
    clause_type: Mapped[str] = enum_col(ClauseType, comment="条款类型")
    contract_type: Mapped[str | None] = enum_col(
        BusinessType, nullable=True, comment="适用业务类型；NULL 表示通用"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="条款标题")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="标准条款正文")
    source: Mapped[str | None] = mapped_column(String(255), comment="来源说明（法条 / 行业惯例）")
    enabled: Mapped[int] = bool_col(default=1)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (Index("idx_type_business", "clause_type", "contract_type"),)


class SubjectBlacklist(Base):
    """主体黑名单：模拟"经营异常名录"查询。

    ⚠️ 本表是 **mock 数据，非真实工商信息**。PRD 要求"主体被列入经营异常"判定，
    但未提供数据源；演示用虚构数据，**不得用于真实业务判断**。
    """

    __tablename__ = "subject_blacklist"

    id: Mapped[int] = pk_col()
    subject_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="主体名称")
    credit_code: Mapped[str | None] = mapped_column(String(32), comment="统一社会信用代码")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="经营异常 / 严重违法失信 / 注销"
    )
    detail: Mapped[str | None] = mapped_column(String(512), comment="详情")
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        Index("idx_subject_name", "subject_name"),
        Index("idx_credit_code", "credit_code"),
    )
