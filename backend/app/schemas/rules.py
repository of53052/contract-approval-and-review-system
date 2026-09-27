"""规则与知识库的请求 / 响应模型。

阶段一**只读**（architecture.md §18.1）；阶段二批次 7 起开放**维护**
（PRD 2.4.3「维护审查规则项、触发条件、风险分级与标准示范条款库」），
因此这里同时有响应模型与写入模型。

**写入模型不含 id / seq**：`id` 由数据库分配；条件的 `seq` 由列表下标决定，
调用方只需保证顺序，不必自己维护编号（编号错乱是人工维护的常见错误源）。
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


# ==================== 写入模型 ====================

class RuleConditionIn(BaseModel):
    """规则条件（写入）。"""

    field: str = Field(min_length=1, max_length=64, description="如 clause.content")
    operator: str
    value: str | None = None
    value_type: str = "string"


class RuleIn(BaseModel):
    """新建规则。"""

    template_id: int
    code: str = Field(min_length=1, max_length=64, description="规则编码，模板内唯一")
    name: str = Field(min_length=1, max_length=128)
    category: str
    risk_level: str
    rule_type: str
    config: dict | None = None
    result_template: str | None = None
    suggestion_template: str | None = None
    enabled: bool = True
    seq: int = 0
    conditions: list[RuleConditionIn] = Field(default_factory=list)


class RuleUpdateIn(BaseModel):
    """更新规则。**未提供的字段不改**；`conditions` 若提供则整体替换。"""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category: str | None = None
    risk_level: str | None = None
    rule_type: str | None = None
    config: dict | None = None
    result_template: str | None = None
    suggestion_template: str | None = None
    enabled: bool | None = None
    seq: int | None = None
    conditions: list[RuleConditionIn] | None = None


class RuleTemplateUpdateIn(BaseModel):
    """更新规则模板。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class StandardClauseOut(BaseModel):
    """标准示范条款。"""

    id: int
    clause_type: str
    contract_type: str | None = None
    title: str
    content: str
    source: str | None = None
    enabled: bool


class StandardClauseIn(BaseModel):
    """新建标准示范条款。"""

    clause_type: str
    contract_type: str | None = None
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    source: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class StandardClauseUpdateIn(BaseModel):
    """更新标准示范条款（未提供的字段不改）。"""

    clause_type: str | None = None
    contract_type: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    source: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class RuleOptionsOut(BaseModel):
    """规则配置页的下拉选项。

    **为什么由后端下发而不是前端硬编码**：枚举值一旦前后端漂移，
    用户能选到后端不认识的值，保存后才在审查时静默失效——
    与 `risk_level` 参数名漂移同类。这里一次下发，杜绝该风险。
    """

    rule_type: list[dict]
    operator: list[dict]
    value_type: list[dict]
    category: list[dict]
    risk_level: list[dict]
    clause_type: list[dict]
    metadata_key: list[dict]
    metric: list[dict]


class BlacklistOut(BaseModel):
    """主体黑名单条目。

    ⚠️ **mock 数据，非真实工商信息**（docs/data-model.md §5.18）。
    """

    id: int
    subject_name: str
    credit_code: str | None = None
    status: str
    detail: str | None = None
