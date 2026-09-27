"""全局校验与防幻觉闸门。

设计依据：docs/architecture.md §8.6（必备条款全局校验）、§8.7（防幻觉闸门）。

两个职责：

1. **全局校验**：长合同分块后，"必备条款缺失"是全局判断，单块审查天然看不见。
   这里在全文层面统一校验。

2. **防幻觉闸门**：对 LLM 产出的每一条风险执行三项检查——
   JSON 可解析（在 provider 层已做）、引用可锚定、法条可核验。
   无法通过的项**必须显式标记**，不得静默丢弃或伪造位置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.config_models import StandardClause
from app.models.enums import MetadataKey
from app.services.parsing.anchor_builder import AnchorBuilder, AnchorResult
from app.services.review.clause_splitter import ClauseDraft, MetadataDraft
from app.services.review.merger import MergedRisk

logger = logging.getLogger(__name__)


@dataclass
class AnchoredRisk:
    """完成锚定与闸门检查的风险项。"""

    risk: MergedRisk
    anchor: AnchorResult | None
    #: 引用无法锚定时为 True，落库到 `risk_item.unanchored`
    unanchored: bool


@dataclass
class GateOutcome:
    items: list[AnchoredRisk]
    unanchored_count: int = 0
    need_review_count: int = 0


class HallucinationGate:
    """防幻觉闸门：锚定检查 + 法条核验。"""

    def __init__(
        self,
        builder: AnchorBuilder,
        clauses: list[ClauseDraft],
        db: Session | None = None,
    ) -> None:
        """`clauses` 是**必填**的。

        `clause_index` 是 `clauses` 列表的下标，**不是** `doc.blocks` 的下标。
        两者长度与含义都不同（实测 10 条条款 vs 25 个块），混用会把风险锚定到
        完全无关的段落。把它设为可选参数会带来"忘了传 → 静默降级为无法锚定"
        的隐患，因此强制调用方显式传入。
        """
        self.builder = builder
        self.clauses = clauses
        self.db = db
        self._standard_titles: set[str] | None = None

    def apply(self, risks: list[MergedRisk]) -> GateOutcome:
        """对全部风险项执行闸门检查。"""
        outcome = GateOutcome(items=[])
        for risk in risks:
            anchor = self._anchor(risk)
            unanchored = anchor is None or not anchor.anchored
            if unanchored:
                logger.info("风险项无法锚定原文，标记 unanchored: %r", risk.title)
                outcome.unanchored_count += 1

            self._check_legal_basis(risk)
            if any(e.need_review for e in risk.evidences):
                outcome.need_review_count += 1

            outcome.items.append(AnchoredRisk(
                risk=risk, anchor=anchor, unanchored=unanchored
            ))

        logger.info(
            "闸门检查完成: %s 项，无法锚定 %s 项，依据待复核 %s 项",
            len(outcome.items), outcome.unanchored_count, outcome.need_review_count,
        )
        return outcome

    def _anchor(self, risk: MergedRisk) -> AnchorResult | None:
        """尝试锚定风险的引用片段。

        引用为空时退回"按条款定位"——规则命中可能没给出精确引用
        （如存在性检查），但知道它属于哪条条款。
        """
        if risk.quote:
            result = self.builder.locate(risk.quote)
            if result.anchored:
                return result

        if risk.clause_index is not None:
            if 0 <= risk.clause_index < len(self.clauses):
                clause = self.clauses[risk.clause_index]
                return self.builder.locate_block(clause.page_no, clause.para_index)
            logger.warning(
                "clause_index %s 超出条款范围 [0, %s)，无法按条款锚定",
                risk.clause_index, len(self.clauses),
            )
        return None

    def _check_legal_basis(self, risk: MergedRisk) -> None:
        """核验法律依据是否在知识库内。

        §8.7 硬规则 3：法条要么限定知识库选取，要么显式标注"AI 生成，需人工复核"。
        落地方式：LLM 给出的法条若无法在 `standard_clause.source` 中匹配，
        则该条依据 `need_review = 1`。
        """
        if not risk.legal_basis:
            return
        titles = self._load_standard_sources()
        basis = risk.legal_basis
        matched = any(
            t and (t in basis or basis in t) for t in titles
        )
        if matched:
            # 命中知识库，可以撤销"待复核"标记
            for e in risk.evidences:
                if e.evidence_type == "llm":
                    e.need_review = False
            return
        logger.info("法条未在知识库中匹配，标记待人工复核: %r", basis[:60])
        for e in risk.evidences:
            if e.evidence_type == "llm":
                e.need_review = True

    def _load_standard_sources(self) -> set[str]:
        """加载知识库中的法条来源标识（只查一次）。"""
        if self._standard_titles is not None:
            return self._standard_titles
        if self.db is None:
            self._standard_titles = set()
            return self._standard_titles
        rows = self.db.execute(select(StandardClause.source)).scalars().all()
        self._standard_titles = {r for r in rows if r}
        return self._standard_titles


# ==================== 必备条款全局校验 ====================

#: 各类合同的必备条款类型。
#: 与 `rule` 表中的 presence 规则**互补**：规则表可配置、可停用，
#: 这里是不可配置的底线清单，保证即使规则被误停也不会漏掉必备项。
REQUIRED_CLAUSE_TYPES: dict[str, tuple[str, ...]] = {
    "purchase": ("subject_matter", "payment", "acceptance", "liability"),
    "sales": ("subject_matter", "payment", "liability"),
    "service": ("subject_matter", "payment", "liability", "confidentiality"),
    "labor": ("payment", "liability"),
}

#: 必备元数据键。缺失即报风险（见 docs/data-model.md §3.5 的"缺失时风险"列）
REQUIRED_METADATA: tuple[tuple[MetadataKey, str, str], ...] = (
    (MetadataKey.PARTY_A_NAME, "high", "甲方名称缺失，无法核实主体资质"),
    (MetadataKey.PARTY_B_NAME, "high", "乙方名称缺失，无法核实主体资质"),
    (MetadataKey.AMOUNT, "medium", "合同金额缺失，比例类规则无法判定"),
    (MetadataKey.CURRENCY, "medium", "币种缺失，涉外场景下金额存在歧义"),
)


def check_global(
    clauses_types: set[str],
    metadata: list[MetadataDraft],
    business_type: str,
) -> list[dict]:
    """全局校验：返回缺失项清单。

    每项含 `kind` / `key` / `risk_level` / `reason`，由调用方转成风险项。
    """
    missing: list[dict] = []
    present_keys = {m.meta_key.value for m in metadata}

    for ctype in REQUIRED_CLAUSE_TYPES.get(business_type, ()):
        if ctype not in clauses_types:
            missing.append({
                "kind": "clause",
                "key": ctype,
                "risk_level": "high",
                "reason": f"合同缺少必备条款类型：{ctype}",
            })

    for key, level, reason in REQUIRED_METADATA:
        if key.value not in present_keys:
            missing.append({
                "kind": "metadata",
                "key": key.value,
                "risk_level": level,
                "reason": reason,
            })

    if missing:
        logger.info("全局校验发现 %s 项缺失: %s", len(missing), [m["key"] for m in missing])
    return missing
