"""双引擎结果合并：取高不取低 + 双来源留痕。

设计依据：docs/architecture.md §8.3。

> **依据**：法律审查场景中"漏报"比"误报"代价大得多，取高是安全侧；
> 同时两个来源都留痕，报告里能写清"规则命中 + AI 研判"两条依据，可解释性保住。

示例：
    合同写「违约金为合同总额的 25%」
      → 规则表阈值 20% → 规则判 中风险
      → LLM 读到上下文 → LLM 判 高风险
      → 最终 = 高风险（取高），报告呈现两条依据

**合并判据**：同一"风险主题"才算同一风险项。判据是
① 同一 `clause_index`，或 ② 标题/引用的归一化相似度超阈值。
不能简单按标题字符串相等——规则用 `rule.name`，LLM 用自己的措辞，两者几乎不会一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.models.enums import MergedBy, RiskLevel
from app.services.parsing.text_normalize import normalize, similarity
from app.services.review.llm_reviewer import LlmRiskDraft
from app.services.review.rule_engine import RuleHit

logger = logging.getLogger(__name__)

#: 判定"同一风险项"的相似度阈值。
#: 取 0.55：规则与 LLM 的措辞差异较大（"违约责任无上限" vs "赔偿责任无上限"），
#: 实测这类同义表述相似度约 0.6~0.7，而不同主题通常在 0.3 以下。
MERGE_SIMILARITY_THRESHOLD = 0.55

#: 风险等级权重，用于取高（与 enums.RISK_LEVEL_WEIGHT 保持一致语义）
_LEVEL_WEIGHT = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}


@dataclass
class MergedRisk:
    """合并后的风险项（未落库）。

    字段与 `risk_item` + `risk_evidence` 两张表对应。
    """

    title: str
    risk_level: RiskLevel
    category: str
    reason: str
    suggestion: str | None
    legal_basis: str | None
    merged_by: MergedBy
    clause_index: int | None
    quote: str | None
    is_global: bool
    #: 依据链：每条对应一行 `risk_evidence`
    evidences: list["MergedEvidence"] = field(default_factory=list)
    #: 展示顺序，合并后统一编号
    seq: int = 0


@dataclass
class MergedEvidence:
    """一条依据，落库到 `risk_evidence`。"""

    evidence_type: str  # rule / llm
    title: str | None
    detail: str
    raw_snippet: str | None = None
    rule_id: int | None = None
    need_review: bool = False


@dataclass
class MergeOutcome:
    risks: list[MergedRisk] = field(default_factory=list)
    #: 合并统计，用于日志与调试
    rule_only: int = 0
    llm_only: int = 0
    both: int = 0
    #: 因等级冲突而"取高"的次数
    level_upgraded: int = 0

    @property
    def overall_risk(self) -> RiskLevel:
        """整体风险等级 = 所有风险项中的最高等级。

        无风险项时返回 LOW——"没有发现问题"对应最低等级，
        而不是"未知"（`review_task.overall_risk` 是 NOT NULL 语义）。
        """
        if not self.risks:
            return RiskLevel.LOW
        return max(self.risks, key=lambda r: _LEVEL_WEIGHT[r.risk_level]).risk_level

    def counts(self) -> dict[str, int]:
        """按等级统计，写入 `review_task` 的冗余计数列。"""
        out = {level.value: 0 for level in RiskLevel}
        for r in self.risks:
            out[r.risk_level.value] += 1
        return out


class Merger:
    """把规则命中与 LLM 研判合并为统一风险清单。"""

    def merge(
        self, rule_hits: list[RuleHit], llm_risks: list[LlmRiskDraft]
    ) -> MergeOutcome:
        outcome = MergeOutcome()

        # 以规则命中为骨架：规则更稳定、更可解释，作为合并基准更可控
        merged: list[MergedRisk] = []
        used_llm: set[int] = set()

        for hit in rule_hits:
            item = MergedRisk(
                title=hit.title,
                risk_level=hit.risk_level,
                category=hit.category.value,
                reason=hit.reason,
                suggestion=hit.suggestion,
                legal_basis=None,
                merged_by=MergedBy.RULE,
                clause_index=hit.clause_index,
                quote=hit.quote,
                is_global=hit.clause_index is None,
                evidences=[MergedEvidence(
                    evidence_type="rule",
                    title=hit.rule_name,
                    detail=hit.evidence_detail or f"规则 {hit.rule_code} 命中",
                    rule_id=hit.rule_id,
                )],
            )

            # 找对应的 LLM 风险
            partner_idx = self._find_partner(item, llm_risks, used_llm)
            if partner_idx is not None:
                used_llm.add(partner_idx)
                partner = llm_risks[partner_idx]
                item.merged_by = MergedBy.BOTH
                # 取高不取低
                if _LEVEL_WEIGHT[partner.risk_level] > _LEVEL_WEIGHT[item.risk_level]:
                    logger.info(
                        "取高合并: %r %s -> %s（LLM 研判更严重）",
                        item.title, item.risk_level.value, partner.risk_level.value,
                    )
                    item.risk_level = partner.risk_level
                    outcome.level_upgraded += 1
                # 补齐规则缺失的字段：LLM 的成因描述通常更具体
                if not item.suggestion and partner.suggestion:
                    item.suggestion = partner.suggestion
                if partner.legal_basis:
                    item.legal_basis = partner.legal_basis
                item.evidences.append(MergedEvidence(
                    evidence_type="llm",
                    title=partner.title,
                    detail=partner.reason,
                    raw_snippet=partner.raw_snippet,
                    need_review=partner.need_review,
                ))
                outcome.both += 1
            else:
                outcome.rule_only += 1

            merged.append(item)

        # LLM 独有的风险
        for i, risk in enumerate(llm_risks):
            if i in used_llm:
                continue
            merged.append(MergedRisk(
                title=risk.title,
                risk_level=risk.risk_level,
                category=risk.category.value,
                reason=risk.reason,
                suggestion=risk.suggestion,
                legal_basis=risk.legal_basis,
                merged_by=MergedBy.LLM,
                clause_index=risk.clause_index,
                quote=risk.quote,
                is_global=risk.clause_index is None,
                evidences=[MergedEvidence(
                    evidence_type="llm",
                    title=risk.title,
                    detail=risk.reason,
                    raw_snippet=risk.raw_snippet,
                    need_review=risk.need_review,
                )],
            ))
            outcome.llm_only += 1

        # 排序并固化 seq：高风险优先，同等级按页码（条款顺序）
        merged.sort(key=lambda r: (
            -_LEVEL_WEIGHT[r.risk_level],
            r.clause_index if r.clause_index is not None else -1,
        ))
        for i, item in enumerate(merged):
            item.seq = i

        outcome.risks = merged
        logger.info(
            "合并完成: 共 %s 项（规则独有 %s / LLM 独有 %s / 双来源 %s），"
            "取高升级 %s 次，整体等级 %s",
            len(merged), outcome.rule_only, outcome.llm_only,
            outcome.both, outcome.level_upgraded, outcome.overall_risk.value,
        )
        return outcome

    # ---------------- 配对 ----------------

    def _find_partner(
        self, item: MergedRisk, llm_risks: list[LlmRiskDraft], used: set[int]
    ) -> int | None:
        """为规则命中找对应的 LLM 风险。

        判据优先级：
        1. 同一 `clause_index`（最可靠）
        2. 标题相似度超阈值
        3. 引用文本相似度超阈值
        """
        # ① 同条款
        if item.clause_index is not None:
            for i, risk in enumerate(llm_risks):
                if i in used:
                    continue
                if risk.clause_index == item.clause_index:
                    return i

        # ② 标题相似
        base_title = normalize(item.title)
        best_idx, best_score = None, 0.0
        for i, risk in enumerate(llm_risks):
            if i in used:
                continue
            score = similarity(base_title, normalize(risk.title))
            if score > best_score:
                best_score, best_idx = score, i
        if best_idx is not None and best_score >= MERGE_SIMILARITY_THRESHOLD:
            return best_idx

        # ③ 引用相似
        if item.quote:
            base_quote = normalize(item.quote)
            best_idx, best_score = None, 0.0
            for i, risk in enumerate(llm_risks):
                if i in used or not risk.quote:
                    continue
                score = similarity(base_quote, normalize(risk.quote))
                if score > best_score:
                    best_score, best_idx = score, i
            if best_idx is not None and best_score >= MERGE_SIMILARITY_THRESHOLD:
                return best_idx

        return None
