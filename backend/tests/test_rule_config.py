"""规则配置写入校验测试。

设计依据：PRD 2.4.3（维护审查规则项、触发条件、风险分级与标准示范条款库）；
`app/services/review/rule_config.py` 的模块 docstring。

**为什么必须测这一层**：规则引擎对坏规则是 `logger.error` 后 `continue`，
所以配置错误的唯一可观测后果是"审查漏报"——没人会注意到。
把校验测厚，等于把"静默失效"变成"保存时报错"。
"""

from __future__ import annotations

import pytest

from sqlalchemy import select

from app.models import Rule
from app.models.enums import (
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
    ValueType,
)
from app.services.review.rule_config import (
    RuleConfigError,
    RuleSpec,
    validate_rule,
    validate_standard_clause,
)


def _spec(**kw) -> RuleSpec:
    """构造一个默认合法的关键词规则，便于单点覆盖。"""
    base = dict(
        code="TEST_RULE",
        name="测试规则",
        category=RiskCategory.LIABILITY.value,
        risk_level=RiskLevel.HIGH.value,
        rule_type=RuleType.KEYWORD.value,
        config={"keywords": ["无上限"]},
        conditions=[{"field": "clause.content", "operator": "contains",
                     "value": "无上限", "value_type": "string"}],
    )
    base.update(kw)
    return RuleSpec(**base)


# ==================== 基础字段 ====================

def test_valid_rule_passes() -> None:
    validate_rule(_spec())  # 不抛异常即通过


@pytest.mark.parametrize("code", ["lower_case", "有中文", "1ABC", "A-B", ""])
def test_invalid_code_rejected(code: str) -> None:
    """编码进日志与依据链，必须是大写字母/数字/下划线。"""
    with pytest.raises(RuleConfigError, match="规则编码"):
        validate_rule(_spec(code=code))


def test_unknown_enum_rejected_with_options() -> None:
    """报错要列出可选值，否则用户只能猜。"""
    with pytest.raises(RuleConfigError) as ei:
        validate_rule(_spec(risk_level="critical"))
    assert "高风险" in str(ei.value) or "high" in str(ei.value)


# ==================== 条件 ====================

def test_condition_field_must_be_engine_known() -> None:
    """引擎只认 clause.* / metadata.* 前缀，写别的等于没写。"""
    with pytest.raises(RuleConfigError, match="无法被引擎解释"):
        validate_rule(_spec(conditions=[
            {"field": "contract.amount", "operator": "contains", "value": "x"},
        ]))


def test_condition_clause_key_must_be_known() -> None:
    with pytest.raises(RuleConfigError, match="clause 字段只支持"):
        validate_rule(_spec(conditions=[
            {"field": "clause.whatever", "operator": "contains", "value": "x"},
        ]))


def test_condition_metadata_key_must_be_known() -> None:
    with pytest.raises(RuleConfigError, match="metadata 字段只支持"):
        validate_rule(_spec(conditions=[
            {"field": "metadata.not_a_key", "operator": "exists"},
        ]))


def test_exists_operator_rejects_value() -> None:
    """exists / not_exists 是存在性判断，带值说明用户填错了位置。"""
    with pytest.raises(RuleConfigError, match="不接受比较值"):
        validate_rule(_spec(conditions=[
            {"field": "metadata.amount", "operator": "exists", "value": "有"},
        ]))


def test_contains_operator_requires_value() -> None:
    with pytest.raises(RuleConfigError, match="必须提供比较值"):
        validate_rule(_spec(conditions=[
            {"field": "clause.content", "operator": "contains", "value": ""},
        ]))


def test_invalid_regex_rejected_at_write_time() -> None:
    """引擎运行期会吞掉非法正则，保存时不拦住就是永久不生效。"""
    with pytest.raises(RuleConfigError, match="正则表达式非法"):
        validate_rule(_spec(
            rule_type=RuleType.REGEX.value, config=None,
            conditions=[{"field": "clause.content", "operator": "regex",
                         "value": "(未闭合", "value_type": "regex"}],
        ))


# ==================== 各规则类型的必填参数 ====================

def test_keyword_rule_needs_keywords() -> None:
    with pytest.raises(RuleConfigError, match="没有任何关键词"):
        validate_rule(_spec(config={}, conditions=[]))


def test_regex_rule_needs_patterns() -> None:
    with pytest.raises(RuleConfigError, match="没有任何模式"):
        validate_rule(_spec(rule_type=RuleType.REGEX.value, config={}, conditions=[]))


def test_threshold_rule_needs_known_metric() -> None:
    with pytest.raises(RuleConfigError, match="缺少可用的 metric"):
        validate_rule(_spec(
            rule_type=RuleType.THRESHOLD.value,
            config={"metric": "whatever", "threshold": 0.2}, conditions=[],
        ))


def test_threshold_rule_needs_numeric_threshold() -> None:
    with pytest.raises(RuleConfigError, match="必须是数字"):
        validate_rule(_spec(
            rule_type=RuleType.THRESHOLD.value,
            config={"metric": "penalty_ratio", "threshold": "两成"}, conditions=[],
        ))


def test_threshold_rule_rejects_bad_direction() -> None:
    with pytest.raises(RuleConfigError, match="direction"):
        validate_rule(_spec(
            rule_type=RuleType.THRESHOLD.value,
            config={"metric": "penalty_ratio", "threshold": 0.2, "direction": "above"},
            conditions=[],
        ))


def test_presence_rule_needs_requirements() -> None:
    with pytest.raises(RuleConfigError, match="没有任何要求项"):
        validate_rule(_spec(rule_type=RuleType.PRESENCE.value, config={}, conditions=[]))


def test_presence_within_type_requires_pattern() -> None:
    """`within_clause_type` 只在同时给 required_pattern 时才有效。"""
    with pytest.raises(RuleConfigError, match="required_pattern"):
        validate_rule(_spec(
            rule_type=RuleType.PRESENCE.value,
            config={"within_clause_type": "payment"}, conditions=[],
        ))


def test_presence_accepts_not_exists_condition() -> None:
    """conditions 里的 not_exists 也算要求项（引擎的读取逻辑如此）。"""
    validate_rule(_spec(
        rule_type=RuleType.PRESENCE.value, config={},
        conditions=[{"field": "metadata.currency", "operator": "not_exists"}],
    ))


def test_blacklist_rule_needs_entries() -> None:
    with pytest.raises(RuleConfigError, match="没有任何条目"):
        validate_rule(_spec(rule_type=RuleType.BLACKLIST.value, config={}, conditions=[]))


def test_blacklist_subject_source_is_enough() -> None:
    """走主体库时不需要关键词列表。"""
    validate_rule(_spec(
        rule_type=RuleType.BLACKLIST.value,
        config={"source": "subject_blacklist"}, conditions=[],
    ))


# ==================== 种子规则体检 ====================

def test_seeded_rules_all_pass_validation() -> None:
    """库里所有规则都必须通过校验。

    **这条测试的真实价值**：`NO_ACCEPTANCE_BEFORE_PAY` 曾带一个
    `clause.payment.content not_contains 验收` 条件——引擎从不读这个字段
    （`clause.` 后只能是 content / clause_type / clause_no / title），
    于是它成了死配置：看起来配了，实则毫无作用，而审查结果表面正常。
    这类"配了等于没配"的问题只能靠全量体检发现。
    """
    from app.core.database import SessionLocal

    failures: list[str] = []
    total = 0
    # 会话内构造规格：conditions 是 lazy load，出了会话就无法再取
    with SessionLocal() as db2:
        for r in db2.execute(select(Rule)).scalars():
            total += 1
            spec = RuleSpec(
                code=r.code, name=r.name, category=r.category, risk_level=r.risk_level,
                rule_type=r.rule_type, config=r.config,
                result_template=r.result_template, suggestion_template=r.suggestion_template,
                conditions=[
                    {"field": c.field, "operator": c.operator,
                     "value": c.value, "value_type": c.value_type}
                    for c in sorted(r.conditions, key=lambda x: x.seq)
                ],
            )
            try:
                validate_rule(spec)
            except RuleConfigError as exc:
                failures.append(f"#{r.id} {r.code}: {exc}")

    if total == 0:
        pytest.skip("规则库未初始化（需先跑 scripts/seed_data.py）")
    assert not failures, "存在引擎不会执行的配置：\n" + "\n".join(failures)


# ==================== 标准示范条款 ====================

def test_standard_clause_ok() -> None:
    validate_standard_clause(
        clause_type="liability", contract_type=None,
        title="责任上限", content="任一方赔偿总额不超过合同总金额。",
    )


@pytest.mark.parametrize("bad", ["", "   "])
def test_standard_clause_rejects_blank_title(bad: str) -> None:
    with pytest.raises(RuleConfigError, match="标题不能为空"):
        validate_standard_clause(
            clause_type="liability", contract_type=None, title=bad, content="x"
        )


def test_standard_clause_rejects_blank_content() -> None:
    with pytest.raises(RuleConfigError, match="正文不能为空"):
        validate_standard_clause(
            clause_type="liability", contract_type=None, title="t", content="  "
        )


def test_standard_clause_rejects_unknown_type() -> None:
    with pytest.raises(RuleConfigError, match="条款类型"):
        validate_standard_clause(
            clause_type="nope", contract_type=None, title="t", content="c"
        )
