"""规则配置的写入校验。

设计依据：PRD 2.4.3「维护审查规则项、触发条件、风险分级与标准示范条款库」；
docs/architecture.md §8.2（规则引擎）、§8.5（阈值表）。

**为什么需要一个专门的校验模块**：规则引擎遇到配错的规则会
`logger.error` 后 `continue`（见 `rule_engine.run`）——这是正确的运行时行为
（单条坏规则不该中断整轮审查），但后果是**配错的规则永久静默失效**：
用户在配置页看到"已启用"，审查却从不命中，且没有任何提示。

因此写入路径必须把"引擎会静默忽略"的情况提前变成 400，把问题挡在配置页。
校验规则**直接对应引擎读取的字段**，两边改动时必须同步（见 `_validate_*`）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models.enums import (
    ClauseType,
    MetadataKey,
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
    ValueType,
)

logger = logging.getLogger(__name__)


class RuleConfigError(ValueError):
    """规则配置非法。由路由转成 400。"""


#: 引擎实际支持的 threshold 指标（`rule_engine._apply_threshold`）。
SUPPORTED_METRICS: dict[str, str] = {
    "penalty_ratio": "违约金比例（从条款文本提取百分比）",
    "amount": "合同金额（取 metadata.amount）",
}

#: 条件可作用的字段前缀。引擎按前缀分派（`clause.` / `metadata.`）。
_FIELD_PREFIXES = ("clause.", "metadata.")

#: 条件字段的合法后缀。引擎只读这几个键，写别的等于没写。
_CLAUSE_FIELDS = {"content", "clause_type", "clause_no", "title"}
_METADATA_FIELDS = {k.value for k in MetadataKey}

#: 需要 `value` 的运算符（引擎在这些分支里判 `if cond.value`）。
_OPERATORS_NEEDING_VALUE = {
    RuleOperator.CONTAINS.value,
    RuleOperator.NOT_CONTAINS.value,
    RuleOperator.REGEX.value,
    RuleOperator.GT.value,
    RuleOperator.GTE.value,
    RuleOperator.LT.value,
    RuleOperator.LTE.value,
    RuleOperator.EQ.value,
}

#: 不需要 `value` 的运算符。有值时说明用户填错了位置。
_OPERATORS_WITHOUT_VALUE = {RuleOperator.EXISTS.value, RuleOperator.NOT_EXISTS.value}


@dataclass
class RuleSpec:
    """校验所需的规则字段（与 schema 解耦，便于单测直接构造）。"""

    code: str
    name: str
    category: str
    risk_level: str
    rule_type: str
    config: dict | None = None
    result_template: str | None = None
    suggestion_template: str | None = None
    conditions: list[dict] | None = None


def validate_rule(spec: RuleSpec) -> None:
    """校验一条规则，非法时抛 `RuleConfigError`。

    只做**会导致引擎静默失效**的检查，不做风格约束。
    """
    if not spec.code.strip():
        raise RuleConfigError("规则编码不能为空")
    # 编码进日志与依据链，保持 ASCII 大写下划线便于检索
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", spec.code):
        raise RuleConfigError(
            f"规则编码 {spec.code!r} 不合法：只允许大写字母、数字与下划线，且以字母开头"
        )

    _check_enum(spec.category, RiskCategory, "风险分类")
    _check_enum(spec.risk_level, RiskLevel, "风险等级")
    _check_enum(spec.rule_type, RuleType, "规则类型")

    conditions = spec.conditions or []
    for i, cond in enumerate(conditions):
        _validate_condition(cond, spec.rule_type, index=i)

    _validate_by_type(spec, conditions)


def _check_enum(value: str, enum_cls, label: str) -> None:
    try:
        enum_cls(value)
    except ValueError:
        allowed = "、".join(e.value for e in enum_cls)
        raise RuleConfigError(f"{label} {value!r} 非法，可选：{allowed}") from None


def _validate_condition(cond: dict, rule_type: str, *, index: int) -> None:
    """校验单个条件。"""
    field = (cond.get("field") or "").strip()
    operator = cond.get("operator")
    value = cond.get("value")
    value_type = cond.get("value_type") or ValueType.STRING.value
    where = f"第 {index + 1} 个条件"

    if not field:
        raise RuleConfigError(f"{where}：作用字段不能为空")
    if not any(field.startswith(p) for p in _FIELD_PREFIXES):
        raise RuleConfigError(
            f"{where}：字段 {field!r} 无法被引擎解释，"
            f"必须以 {' 或 '.join(_FIELD_PREFIXES)} 开头"
        )

    prefix, _, key = field.partition(".")
    if prefix == "clause" and key not in _CLAUSE_FIELDS:
        # 这个报错要给出正确写法：`clause.payment.content` 是最容易写出的
        # 直觉式表达（"付款条款的正文"），但引擎从不读它。
        # 正确的表达是 config.within_clause_type + required_pattern。
        hint = (
            "如需限定某类条款（如付款条款），"
            "请用 config.within_clause_type + config.required_pattern 表达"
            if "." in key else ""
        )
        raise RuleConfigError(
            f"{where}：clause 字段只支持 {'、'.join(sorted(_CLAUSE_FIELDS))}，"
            f"收到 {key!r}。{hint}"
        )
    if prefix == "metadata" and key not in _METADATA_FIELDS:
        raise RuleConfigError(
            f"{where}：metadata 字段只支持 {'、'.join(sorted(_METADATA_FIELDS))}，收到 {key!r}"
        )

    _check_enum(operator, RuleOperator, f"{where}的运算符")
    _check_enum(value_type, ValueType, f"{where}的值类型")

    if operator in _OPERATORS_WITHOUT_VALUE:
        if value not in (None, ""):
            raise RuleConfigError(
                f"{where}：运算符 {operator} 不接受比较值，请清空 value"
                "（存在性判断只看字段有无）"
            )
    elif operator in _OPERATORS_NEEDING_VALUE and not value:
        raise RuleConfigError(f"{where}：运算符 {operator} 必须提供比较值")

    if value_type == ValueType.REGEX.value or operator == RuleOperator.REGEX.value:
        _check_regex(value, where)


def _check_regex(pattern: str | None, where: str) -> None:
    """正则必须在保存时就编译通过。

    引擎在运行期用 `try/except re.error` 吞掉非法正则（只打日志），
    等于该条件永不生效——放到写入期报错，用户能立刻改。
    """
    if not pattern:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise RuleConfigError(f"{where}：正则表达式非法（{exc}）") from exc


def _validate_by_type(spec: RuleSpec, conditions: list[dict]) -> None:
    """按规则类型校验其专属参数。对应 `rule_engine` 各 `_apply_*` 的读取逻辑。"""
    config = spec.config or {}
    rtype = spec.rule_type

    if rtype == RuleType.KEYWORD.value:
        keywords = list(config.get("keywords") or [])
        cond_values = [
            c.get("value") for c in conditions
            if c.get("operator") == RuleOperator.CONTAINS.value and c.get("value")
        ]
        if not keywords and not cond_values:
            raise RuleConfigError(
                "关键词规则没有任何关键词：请在 config.keywords 或条件（contains）中至少给一个"
            )

    elif rtype == RuleType.REGEX.value:
        patterns = [
            c.get("value") for c in conditions
            if c.get("operator") == RuleOperator.REGEX.value and c.get("value")
        ] or list(config.get("patterns") or [])
        if not patterns:
            raise RuleConfigError(
                "正则规则没有任何模式：请在条件（regex）或 config.patterns 中至少给一个"
            )

    elif rtype == RuleType.THRESHOLD.value:
        metric = config.get("metric")
        if metric not in SUPPORTED_METRICS:
            raise RuleConfigError(
                f"阈值规则缺少可用的 metric，可选：{'、'.join(SUPPORTED_METRICS)}"
            )
        if config.get("threshold") is None:
            raise RuleConfigError("阈值规则必须提供 config.threshold")
        try:
            float(config["threshold"])
        except (TypeError, ValueError):
            raise RuleConfigError(
                f"config.threshold 必须是数字，收到 {config['threshold']!r}"
            ) from None
        direction = config.get("direction", "gt")
        if direction not in {"gt", "gte", "lt", "lte", "eq"}:
            raise RuleConfigError(
                f"config.direction {direction!r} 非法，可选：gt / gte / lt / lte / eq"
            )

    elif rtype == RuleType.PRESENCE.value:
        required_types = config.get("required_clause_type")
        required_types = (
            [required_types] if isinstance(required_types, str) else list(required_types or [])
        )
        required_keys = list(config.get("required_keys") or [])
        required_pattern = config.get("required_pattern")

        for t in required_types:
            _check_enum(t, ClauseType, "config.required_clause_type")
        for k in required_keys:
            _check_enum(k, MetadataKey, "config.required_keys")

        # conditions 里的 not_exists 也会被引擎当成"必须存在"（见 _apply_presence）
        cond_requirements = [
            c for c in conditions
            if c.get("operator") == RuleOperator.NOT_EXISTS.value
        ]

        if not (required_types or required_keys or required_pattern or cond_requirements):
            raise RuleConfigError(
                "存在性规则没有任何要求项："
                "请填写 config.required_clause_type / required_keys / required_pattern，"
                "或添加 not_exists 条件"
            )

        # 指定了 within_clause_type 才有"条款内必须出现某模式"的语义
        within = config.get("within_clause_type")
        if within is not None:
            _check_enum(within, ClauseType, "config.within_clause_type")
            if not required_pattern:
                raise RuleConfigError(
                    "config.within_clause_type 只在同时提供 config.required_pattern 时生效，"
                    "当前缺少 required_pattern"
                )

    elif rtype == RuleType.BLACKLIST.value:
        if config.get("source") == "subject_blacklist":
            return
        entries = list(config.get("blacklist") or [])
        cond_values = [
            c.get("value") for c in conditions
            if c.get("operator") in (RuleOperator.CONTAINS.value, RuleOperator.REGEX.value)
            and c.get("value")
        ]
        if not entries and not cond_values:
            raise RuleConfigError(
                "黑名单规则没有任何条目：请填写 config.blacklist，"
                "或把 config.source 设为 subject_blacklist"
            )


def validate_standard_clause(
    *, clause_type: str, contract_type: str | None, title: str, content: str
) -> None:
    """校验标准示范条款。"""
    from app.models.enums import BusinessType

    _check_enum(clause_type, ClauseType, "条款类型")
    if contract_type is not None:
        _check_enum(contract_type, BusinessType, "适用业务类型")
    if not title.strip():
        raise RuleConfigError("条款标题不能为空")
    if not content.strip():
        raise RuleConfigError("条款正文不能为空")
