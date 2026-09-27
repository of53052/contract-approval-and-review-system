"""确定性规则引擎。

设计依据：docs/architecture.md §8.2（职责边界）、§8.5（阈值表）。

**职责**：金额、违约金比例、管辖地关键词、必备条款存在性、主体黑名单。
特点是**可复现、可解释**，能进报告当"法律依据"。

**关键词匹配在应用层执行，不下推给数据库**（§11 索引汇总的结论）：
MySQL 的 FULLTEXT 默认按空格分词，对中文等于不可用。

规则定义来自 `rule` / `rule_condition` 表，本模块只负责**执行**，
不含硬编码的规则内容——阈值改配置即可，无需改代码。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.config_models import Rule, RuleCondition
from app.models.enums import (
    ClauseType,
    MetadataKey,
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
)
from app.services.review.clause_splitter import ClauseDraft, MetadataDraft

logger = logging.getLogger(__name__)


@dataclass
class RuleHit:
    """规则命中结果（未落库）。

    `clause_index` 指向 `clauses` 列表下标；全局性问题（如必备条款缺失）为 None。
    """

    rule_code: str
    rule_name: str
    category: RiskCategory
    risk_level: RiskLevel
    title: str
    reason: str
    suggestion: str | None
    clause_index: int | None
    #: 命中的原文片段，用于锚点定位
    quote: str | None
    rule_id: int | None = None
    #: 规则条件求值的中间结果，落库到 `risk_evidence.detail`
    evidence_detail: str = ""


@dataclass
class RuleEngineInput:
    """规则引擎输入。"""

    clauses: list[ClauseDraft]
    metadata: list[MetadataDraft]
    #: 主体黑名单命中结果（由调用方查库后传入，引擎不直接访问数据库）
    blacklist_hits: dict[str, str] = field(default_factory=dict)
    #: 该合同类型下启用的规则
    rules: list[Rule] = field(default_factory=list)


class RuleEngine:
    """执行规则并产出命中项。"""

    def __init__(self, inp: RuleEngineInput) -> None:
        self.inp = inp
        self._meta = {m.meta_key.value: m for m in inp.metadata}
        self._full_text = "\n".join(c.content for c in inp.clauses)

    def run(self) -> list[RuleHit]:
        """按 `seq` 顺序执行全部启用的规则。"""
        hits: list[RuleHit] = []
        rules = sorted(
            [r for r in self.inp.rules if r.enabled],
            key=lambda r: (r.seq, r.id or 0),
        )
        for rule in rules:
            try:
                hit = self._apply(rule)
            except Exception as exc:  # noqa: BLE001 - 单条规则异常不应中断整轮审查
                logger.error("规则 %s 执行失败: %s", rule.code, exc, exc_info=True)
                continue
            if hit is not None:
                hits.append(hit)
        logger.info("规则引擎完成: %s 条规则, %s 项命中", len(rules), len(hits))
        return hits

    # ---------------- 规则分派 ----------------

    def _apply(self, rule: Rule) -> RuleHit | None:
        """按 `rule_type` 分派到具体实现。"""
        rtype = RuleType(rule.rule_type)
        if rtype is RuleType.KEYWORD:
            return self._apply_keyword(rule)
        if rtype is RuleType.REGEX:
            return self._apply_regex(rule)
        if rtype is RuleType.THRESHOLD:
            return self._apply_threshold(rule)
        if rtype is RuleType.PRESENCE:
            return self._apply_presence(rule)
        if rtype is RuleType.BLACKLIST:
            return self._apply_blacklist(rule)
        logger.warning("未知规则类型 %s（规则 %s）", rule.rule_type, rule.code)
        return None

    # ---------------- 各类型实现 ----------------

    def _apply_keyword(self, rule: Rule) -> RuleHit | None:
        """关键词匹配。

        `config.match_all = True` 时要求全部关键词出现（AND），
        否则任一出现即命中（OR）。

        **关键词有两个来源，取并集**：
        - `config.keywords`：类型相关参数，适合放"同义变体列表"
        - `rule_condition`（operator=contains）：规则的结构化条件定义

        两者都读是为了避免"conditions 表成了摆设"——文档 §5.16 把条件
        定义在 conditions 表里，若引擎只认 config，维护者改表不生效，
        会产生"改了配置没反应"的隐蔽问题。
        """
        config = rule.config or {}
        keywords: list[str] = list(config.get("keywords") or [])
        for cond in self._conditions(rule):
            if cond.operator == RuleOperator.CONTAINS and cond.value:
                if cond.value not in keywords:
                    keywords.append(cond.value)
        if not keywords:
            return None
        match_all = bool(config.get("match_all", False))

        for idx, clause in enumerate(self.inp.clauses):
            found = [kw for kw in keywords if kw in clause.content]
            if (match_all and len(found) == len(keywords)) or (not match_all and found):
                return self._make_hit(
                    rule, idx, quote=self._quote_around(clause.content, found[0]),
                    detail=f"关键词命中: {found}（条款 {clause.clause_no or clause.seq}）",
                )
        return None

    def _apply_regex(self, rule: Rule) -> RuleHit | None:
        """正则匹配。正则来自规则条件表，非用户输入，无 ReDoS 风险面。"""
        patterns = [
            c.value for c in self._conditions(rule)
            if c.operator == RuleOperator.REGEX and c.value
        ]
        if not patterns:
            patterns = (rule.config or {}).get("patterns") or []
        if not patterns:
            return None

        for idx, clause in enumerate(self.inp.clauses):
            for pat in patterns:
                try:
                    m = re.search(pat, clause.content)
                except re.error as exc:
                    logger.error("规则 %s 的正则非法: %s", rule.code, exc)
                    continue
                if m:
                    return self._make_hit(
                        rule, idx, quote=m.group(0)[:200],
                        detail=f"正则命中: {pat!r} -> {m.group(0)[:60]!r}",
                    )
        return None

    def _apply_threshold(self, rule: Rule) -> RuleHit | None:
        """阈值比较。

        目前支持两类指标：
        - `penalty_ratio`：违约金比例（需从条款里提取比例）
        - `amount`：金额本身（需 metadata.amount）
        """
        config = rule.config or {}
        metric = config.get("metric")
        threshold = config.get("threshold")
        direction = config.get("direction", "gt")
        if threshold is None or metric is None:
            return None

        try:
            threshold_d = Decimal(str(threshold))
        except InvalidOperation:
            logger.error("规则 %s 的 threshold 非法: %r", rule.code, threshold)
            return None

        if metric == "penalty_ratio":
            return self._check_penalty_ratio(rule, threshold_d, direction)
        if metric == "amount":
            return self._check_amount(rule, threshold_d, direction)
        logger.warning("未知 threshold metric: %s（规则 %s）", metric, rule.code)
        return None

    def _check_penalty_ratio(
        self, rule: Rule, threshold: Decimal, direction: str
    ) -> RuleHit | None:
        """检查违约金比例。

        从条款文本里提取百分比（如"25%"、"百分之二十五"），
        与阈值比较。**比例优先于金额**：合同常写比例而非绝对额。
        """
        for idx, clause in enumerate(self.inp.clauses):
            ratio = _extract_ratio(clause.content)
            if ratio is None:
                continue
            if _compare(ratio, threshold, direction):
                return self._make_hit(
                    rule, idx, quote=self._quote_around(clause.content, "%"),
                    detail=(f"违约金比例 {ratio}% 触发阈值 "
                            f"{threshold}%（条件 {direction}）"),
                )
        return None

    def _check_amount(
        self, rule: Rule, threshold: Decimal, direction: str
    ) -> RuleHit | None:
        """检查合同金额。"""
        m = self._meta.get(MetadataKey.AMOUNT.value)
        if m is None or m.value_normalized is None:
            return None
        try:
            amount = Decimal(m.value_normalized)
        except InvalidOperation:
            return None
        if _compare(amount, threshold, direction):
            return self._make_hit(
                rule, None, quote=m.meta_value,
                detail=f"金额 {amount} 触发阈值 {threshold}（条件 {direction}）",
            )
        return None

    def _apply_presence(self, rule: Rule) -> RuleHit | None:
        """存在性检查（必备条款 / 必备元数据）。

        这是**全局判断**：长合同分块后单块看不见"缺某条款"，
        因此这里扫全文而非逐块判定（见 §8.6）。

        要求项有两个来源（取并集，理由同 `_apply_keyword`）：
        - `config`：`required_clause_type` / `required_keys` / `required_pattern`
        - `rule_condition`：`not_exists` 条件，语义即"必须存在"
        """
        config = rule.config or {}

        # --- 必备条款类型 ---
        raw_types = config.get("required_clause_type")
        if raw_types is None:
            required_types: list[str] = []
        elif isinstance(raw_types, str):
            required_types = [raw_types]
        else:
            required_types = list(raw_types)

        # --- 必备元数据键 ---
        required_keys: list[str] = list(config.get("required_keys") or [])

        # --- 必须出现的文本模式 ---
        required_pattern = config.get("required_pattern")

        # 合并 conditions 中的 not_exists 条件
        for cond in self._conditions(rule):
            if cond.operator != RuleOperator.NOT_EXISTS or not cond.field:
                continue
            key = cond.field.split(".")[-1]
            if cond.field.startswith("clause."):
                # `clause.clause_type` 的语义是"某类条款必须存在"，
                # 具体是哪个类型由 `config.required_clause_type` 给出。
                # 不能把字段名 `clause_type` 直接当成条款类型值
                # （实测缺陷：报出"缺失 clause_type"这种无意义的检查项）。
                if key != "clause_type":
                    logger.warning(
                        "规则 %s 的 not_exists 条件字段 %r 无法解释，已忽略",
                        rule.code, cond.field,
                    )
            elif cond.field.startswith("metadata.") and key not in required_keys:
                required_keys.append(key)

        missing: list[str] = []

        if required_types:
            present = {c.clause_type.value for c in self.inp.clauses}
            missing += [t for t in required_types if t not in present]

        if required_keys:
            missing += [k for k in required_keys if k not in self._meta]

        # --- 指定条款类型内的必须模式 ---
        # 语义：在 `within_clause_type` 类条款的正文里，必须出现 `required_pattern`。
        #
        # **为什么需要这个组合**（实测发现的规则表达力缺口）：
        # PRD 要求判定"付款未以验收为前置条件"。这**不等于**"合同没有验收条款"——
        # 实测样本里同时存在「第四条 验收标准」与「到货即付全款」，
        # 用"验收条款是否存在"判定会漏报；正确语义是"付款条款内必须提到验收"。
        within = config.get("within_clause_type")
        if required_pattern and within:
            targets = [
                c for c in self.inp.clauses
                if c.clause_type.value == within
            ]
            if targets and not any(required_pattern in c.content for c in targets):
                missing.append(
                    f"{within} 类条款内未出现 {required_pattern!r}"
                )
        elif required_pattern and required_pattern not in self._full_text:
            missing.append(f"未出现 {required_pattern!r}")

        if not missing:
            return None

        # 尽量给出条款下标：虽然是"全局判断"，但缺失项往往对应某个
        # 确实存在的条款（如"保密条款没写期限"），锚定过去能让前端
        # 直接跳到相关段落，而不是只能显示"无法定位"。
        clause_index = self._guess_anchor_clause(rule, required_types, required_pattern)
        return self._make_hit(
            rule, clause_index, detail=f"存在性检查失败，缺失: {missing}"
        )

    def _guess_anchor_clause(
        self,
        rule: Rule,
        required_types: list[str],
        required_pattern: str | None,
    ) -> int | None:
        """为全局性缺失项推测一个可锚定的条款。

        策略（按可靠性排序）：
        1. `config.anchor_clause_type` 显式指定
        2. `required_types` 里第一个在文档中存在的条款类型
        3. 按风险分类到条款类型的映射（如 confidentiality → confidentiality）
        """
        config = rule.config or {}

        explicit = config.get("anchor_clause_type")
        if explicit:
            idx = self._first_clause_of_type(explicit)
            if idx is not None:
                return idx

        for ctype in required_types:
            idx = self._first_clause_of_type(ctype)
            if idx is not None:
                return idx

        mapped = _CATEGORY_TO_CLAUSE_TYPE.get(rule.category)
        if mapped:
            return self._first_clause_of_type(mapped)
        return None

    def _first_clause_of_type(self, clause_type: str) -> int | None:
        """取指定类型的第一条条款下标。"""
        for idx, clause in enumerate(self.inp.clauses):
            if clause.clause_type.value == clause_type:
                return idx
        return None

    def _apply_blacklist(self, rule: Rule) -> RuleHit | None:
        """黑名单匹配。

        `config.source = 'subject_blacklist'` 时，命中来自调用方查库的结果
        （引擎不直接访问数据库，保持可测试性）。
        """
        config = rule.config or {}
        if config.get("source") == "subject_blacklist":
            for key in (MetadataKey.PARTY_A_NAME.value, MetadataKey.PARTY_B_NAME.value):
                m = self._meta.get(key)
                if m is None:
                    continue
                status = self.inp.blacklist_hits.get(m.meta_value)
                if status:
                    return self._make_hit(
                        rule, None, quote=m.meta_value,
                        detail=f"主体 {m.meta_value!r} 命中黑名单，状态: {status}",
                    )
            return None

        # 否则按 config.blacklist 的关键词列表匹配条款内容
        entries: list[str] = config.get("blacklist") or []
        if not entries:
            return None
        for idx, clause in enumerate(self.inp.clauses):
            for entry in entries:
                if entry in clause.content:
                    return self._make_hit(
                        rule, idx, quote=self._quote_around(clause.content, entry),
                        detail=f"黑名单命中: {entry!r}",
                    )
        return None

    # ---------------- 辅助 ----------------

    def _conditions(self, rule: Rule) -> list[RuleCondition]:
        return sorted(rule.conditions, key=lambda c: (c.seq, c.id or 0))

    def _make_hit(
        self,
        rule: Rule,
        clause_index: int | None,
        *,
        quote: str | None = None,
        detail: str = "",
    ) -> RuleHit:
        """构造命中项，套用规则的结论模板。"""
        clause_no = (
            self.inp.clauses[clause_index].clause_no
            if clause_index is not None
            else None
        )
        ctx = {
            "threshold": (rule.config or {}).get("threshold", ""),
            "clause_no": clause_no or "全局",
            "value": "",
        }
        title = rule.name
        reason = _render_template(rule.result_template, ctx) or rule.name
        suggestion = _render_template(rule.suggestion_template, ctx)
        return RuleHit(
            rule_code=rule.code,
            rule_name=rule.name,
            category=RiskCategory(rule.category),
            risk_level=RiskLevel(rule.risk_level),
            title=title,
            reason=reason,
            suggestion=suggestion,
            clause_index=clause_index,
            quote=quote,
            rule_id=rule.id,
            evidence_detail=detail,
        )

    @staticmethod
    def _quote_around(text: str, keyword: str, radius: int = 40) -> str:
        """取关键词附近的片段作为引用，供锚点定位。"""
        if not keyword:
            return text[:120]
        pos = text.find(keyword)
        if pos < 0:
            return text[:120]
        start = max(0, pos - radius)
        end = min(len(text), pos + len(keyword) + radius)
        return text[start:end]


#: 风险分类 → 条款类型的映射。
#: 用于给"全局性缺失"风险推测可锚定的条款（两者语义部分重叠但不等价，
#: 因此不做双向映射，只在需要时单向查表）。
_CATEGORY_TO_CLAUSE_TYPE: dict[str, str] = {
    RiskCategory.CONFIDENTIALITY.value: ClauseType.CONFIDENTIALITY.value,
    RiskCategory.FORCE_MAJEURE.value: ClauseType.FORCE_MAJEURE.value,
    RiskCategory.ACCEPTANCE.value: ClauseType.ACCEPTANCE.value,
    RiskCategory.INTELLECTUAL_PROPERTY.value: ClauseType.INTELLECTUAL_PROPERTY.value,
    RiskCategory.JURISDICTION.value: ClauseType.JURISDICTION.value,
    RiskCategory.AMOUNT_PAYMENT.value: ClauseType.PAYMENT.value,
    RiskCategory.LIABILITY.value: ClauseType.LIABILITY.value,
    RiskCategory.DATA_SECURITY.value: ClauseType.DATA_SECURITY.value,
}


# ==================== 工具函数 ====================

#: 阿拉伯数字百分比：25%、25 %
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

#: 中文百分比：百分之二十五
_CN_PERCENT_RE = re.compile(r"百分之([一二三四五六七八九十百零〇\d]+)")

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """中文数字转整数（支持到千位，够用于百分比）。"""
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    for ch in text:
        if ch in _CN_DIGITS:
            section = section * 10 + _CN_DIGITS[ch] if section and _CN_DIGITS[ch] < 10 else _CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
        elif ch == "百":
            section = (section or 1) * 100
        elif ch == "千":
            section = (section or 1) * 1000
        else:
            return None
    total += section
    return total or None


def _extract_ratio(text: str) -> Decimal | None:
    """从文本提取百分比数值（返回 25 表示 25%）。"""
    m = _PERCENT_RE.search(text)
    if m:
        try:
            return Decimal(m.group(1))
        except InvalidOperation:
            return None
    m = _CN_PERCENT_RE.search(text)
    if m:
        val = _cn_to_int(m.group(1))
        if val is not None:
            return Decimal(val)
    return None


def _compare(value: Decimal, threshold: Decimal, direction: str) -> bool:
    """按方向比较。"""
    if direction == "gt":
        return value > threshold
    if direction == "gte":
        return value >= threshold
    if direction == "lt":
        return value < threshold
    if direction == "lte":
        return value <= threshold
    if direction == "eq":
        return value == threshold
    logger.warning("未知比较方向: %s", direction)
    return False


def _render_template(template: str | None, ctx: dict[str, Any]) -> str | None:
    """渲染结论模板，替换 `{占位符}`。

    用 `str.format_map` 的宽松子类：模板里出现未知占位符时保留原样，
    不抛 KeyError——模板由人工维护，不应因一个拼写错误让整轮审查失败。
    """
    if not template:
        return None

    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_Safe(ctx))
    except Exception:  # noqa: BLE001 - 模板语法错误时退回原文
        logger.warning("模板渲染失败，返回原文: %r", template[:60])
        return template
