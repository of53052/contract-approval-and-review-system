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
    """完成锚定与闸门检查的风险项。

    `anchors` 是**列表**：一条风险可能落在多个位置——跨页条款按页各一个，
    或将来"同一风险横跨多个条款"的场景。为空表示无法定位。
    """

    risk: MergedRisk
    anchors: list[AnchorResult]
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
            anchors = self._anchor(risk)
            unanchored = not anchors
            if unanchored:
                logger.info("风险项无法锚定原文，标记 unanchored: %r", risk.title)
                outcome.unanchored_count += 1

            self._check_legal_basis(risk)
            if any(e.need_review for e in risk.evidences):
                outcome.need_review_count += 1

            outcome.items.append(AnchoredRisk(
                risk=risk, anchors=anchors, unanchored=unanchored
            ))

        logger.info(
            "闸门检查完成: %s 项，无法锚定 %s 项，依据待复核 %s 项",
            len(outcome.items), outcome.unanchored_count, outcome.need_review_count,
        )
        return outcome

    def _anchor(self, risk: MergedRisk) -> list[AnchorResult]:
        """尝试锚定风险，返回锚点列表（空列表表示无法定位）。

        优先级：
        1. 引用可精确定位 → 用精确锚点（最可信）
        2. 知道属于哪条条款 → **整条条款**（按页切分，覆盖标题与正文）
        3. 条款下标非法 → 记录警告并放弃
        """
        if risk.quote:
            result = self.builder.locate(risk.quote)
            if result.anchored:
                return [result]

        if risk.clause_index is not None:
            if 0 <= risk.clause_index < len(self.clauses):
                clause = self.clauses[risk.clause_index]
                # 整条条款优先：PRESENCE 类规则没有可提取的命中子串，
                # 只锚标题行会让用户以为风险仅涉及那一行。
                whole = self.builder.locate_clause(clause.content)
                if whole:
                    return whole
                # 兜底：条款正文跨页拼接后无法整体定位时，退回起始块
                fallback = self.builder.locate_block(clause.page_no, clause.para_index)
                return [fallback] if fallback.anchored else []
            logger.warning(
                "clause_index %s 超出条款范围 [0, %s)，无法按条款锚定",
                risk.clause_index, len(self.clauses),
            )
        return []

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

#: 必备元数据键，**按业务类型区分**。缺失即报风险
#: （见 docs/data-model.md §3.5 的"缺失时风险"列）。
#:
#: ⚠️ 为什么必须分类型（批次 9 实测缺陷）：`amount` / `currency` 只对**交易类**
#: 合同（采购/销售/服务）有意义。劳动合同根本没有"合同金额"这个字段，
#: 用同一份清单会导致**每份劳动合同都必报"合同金额缺失"**——这类稳定的
#: 假阳性会训练用户忽略告警，比漏报更伤。`party_a/b_name` 则各类合同通用
#: （用人单位与劳动者同样是签约主体）。
REQUIRED_METADATA_COMMON: tuple[tuple[MetadataKey, str, str], ...] = (
    (MetadataKey.PARTY_A_NAME, "high", "甲方名称缺失，无法核实主体资质"),
    (MetadataKey.PARTY_B_NAME, "high", "乙方名称缺失，无法核实主体资质"),
)

#: 交易类合同（有金额与币种）额外要求的元数据。
REQUIRED_METADATA_TRANSACTIONAL: tuple[tuple[MetadataKey, str, str], ...] = (
    (MetadataKey.AMOUNT, "medium", "合同金额缺失，比例类规则无法判定"),
    (MetadataKey.CURRENCY, "medium", "币种缺失，涉外场景下金额存在歧义"),
)

#: 需要金额/币种的业务类型。劳动合同不在其中。
_TRANSACTIONAL_TYPES: frozenset[str] = frozenset({"purchase", "sales", "service"})


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

    required_meta = REQUIRED_METADATA_COMMON
    if business_type in _TRANSACTIONAL_TYPES:
        required_meta = required_meta + REQUIRED_METADATA_TRANSACTIONAL

    for key, level, reason in required_meta:
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
