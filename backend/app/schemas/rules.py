"""规则与知识库响应模型。

阶段一**只读**（architecture.md §18.1：规则配置页推阶段二），
因此这里只有响应模型，没有写入模型。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RuleConditionOut(BaseModel):
    """规则条件。"""

    id: int
    seq: int
    field: str
    operator: str
    value: str | None = None
    value_type: str


class RuleOut(BaseModel):
    """审查规则。"""

    id: int
    template_id: int
    code: str
    name: str
    category: str
    risk_level: str
    rule_type: str
    config: dict | None = None
    result_template: str | None = None
    suggestion_template: str | None = None
    enabled: bool
    seq: int
    conditions: list[RuleConditionOut] = Field(default_factory=list)


class RuleTemplateOut(BaseModel):
    """规则模板（按业务类型组织）。"""

    id: int
    contract_type: str
    name: str
    description: str | None = None
    enabled: bool
    rules: list[RuleOut] = Field(default_factory=list)


class StandardClauseOut(BaseModel):
    """标准示范条款。"""

    id: int
    clause_type: str
    contract_type: str | None = None
    title: str
    content: str
    source: str | None = None
    enabled: bool


class BlacklistOut(BaseModel):
    """主体黑名单条目。

    ⚠️ **mock 数据，非真实工商信息**（docs/data-model.md §5.18）。
    """

    id: int
    subject_name: str
    credit_code: str | None = None
    status: str
    detail: str | None = None
