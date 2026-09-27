"""LLM 语义研判。

设计依据：docs/architecture.md §8.2（职责边界）、§8.7（防幻觉闸门）。

**职责**：权责是否对等、表述是否高危、条款语义冲突。
与规则引擎互补——规则负责确定性判定，LLM 负责语义理解。

**防幻觉三条硬规则**（§8.7）：
1. JSON 解析失败必须重试或报错，不能返回半成品
2. 引用无法锚定的风险项必须标记，不能当作正常结果
3. 法律依据要么限定知识库选取，要么显式标注"AI 生成，需人工复核"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import RiskCategory, RiskLevel
from app.services.llm import ChatMessage, LlmError, LlmJsonError, LLMProvider
from app.services.review.clause_splitter import ClauseDraft

logger = logging.getLogger(__name__)

#: 单次送审的条款数量上限。
#: 超过则分批——长合同一次性塞进上下文会超限，且模型注意力被稀释。
CLAUSES_PER_BATCH = 8

#: 输出 schema 的字段说明（写进提示词，约束模型输出结构）
_RISK_SCHEMA_HINT = """{
  "risks": [
    {
      "title": "风险项简短标题（不超过 20 字）",
      "risk_level": "high | medium | low",
      "category": "subject_qualification | amount_payment | liability | intellectual_property | jurisdiction | confidentiality | data_security | acceptance | force_majeure | other",
      "reason": "风险成因分析，面向业务人员，2-3 句",
      "legal_basis": "相关法律条文（如不确定请留空字符串）",
      "suggestion": "推荐修改后的条款文本",
      "quote": "合同中支持该判断的**原文片段**，必须逐字复制，不得改写"
    }
  ]
}"""

SYSTEM_PROMPT = """你是一名资深企业法务，负责审查合同风险。

要求：
1. **只输出 JSON**，不要 markdown 代码块标记，不要任何解释性文字
2. `quote` 字段必须**逐字复制**合同原文，不得改写、补全或润色
3. 只报告确实存在的风险；没有风险时返回空数组
4. `legal_basis` 若不确信具体条文，请留空字符串，**不要编造法条**

输出格式：
""" + _RISK_SCHEMA_HINT


@dataclass
class LlmRiskDraft:
    """LLM 研判出的一条风险（未落库）。"""

    title: str
    risk_level: RiskLevel
    category: RiskCategory
    reason: str
    suggestion: str | None
    legal_basis: str | None
    quote: str | None
    clause_index: int | None
    #: LLM 原始输出片段，落库到 `risk_evidence.raw_snippet`
    raw_snippet: str = ""
    #: 法条无法在知识库中匹配时置 True，落库到 `risk_evidence.need_review`
    need_review: bool = False


@dataclass
class LlmReviewOutcome:
    risks: list[LlmRiskDraft] = field(default_factory=list)
    #: 本次研判的统计信息，用于 llm_call_log
    total_retries: int = 0
    batches: int = 0
    failures: list[str] = field(default_factory=list)


class LlmReviewer:
    """条款语义研判。"""

    def __init__(self, provider: LLMProvider, *, batch_size: int = CLAUSES_PER_BATCH) -> None:
        self.provider = provider
        self.batch_size = batch_size

    def review(self, clauses: list[ClauseDraft]) -> LlmReviewOutcome:
        """分批送审全部条款。

        单批失败**不中断整体**：记录失败原因继续下一批，
        最终结果里带上 failures 供上层决定是否降级。
        """
        outcome = LlmReviewOutcome()
        if not clauses:
            return outcome

        for start in range(0, len(clauses), self.batch_size):
            batch = clauses[start : start + self.batch_size]
            outcome.batches += 1
            try:
                risks, retries = self._review_batch(batch, start)
                outcome.risks.extend(risks)
                outcome.total_retries += retries
            except LlmJsonError as exc:
                logger.error("第 %s 批 LLM 输出无法解析为 JSON: %s", outcome.batches, exc)
                outcome.failures.append(f"批次 {outcome.batches} JSON 解析失败: {exc}")
            except LlmError as exc:
                logger.error("第 %s 批 LLM 调用失败: %s", outcome.batches, exc)
                outcome.failures.append(f"批次 {outcome.batches} 调用失败: {exc}")

        logger.info(
            "LLM 研判完成: %s 批, %s 条风险, 重试 %s 次, 失败 %s 批",
            outcome.batches, len(outcome.risks), outcome.total_retries, len(outcome.failures),
        )
        return outcome

    def _review_batch(
        self, batch: list[ClauseDraft], offset: int
    ) -> tuple[list[LlmRiskDraft], int]:
        """送审一批条款，返回 (风险列表, 重试次数)。"""
        user_content = self._build_user_prompt(batch)
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_content),
        ]

        data, meta = self.provider.chat_json(messages, validator=_validate_shape)

        raw = meta.raw or ""
        risks: list[LlmRiskDraft] = []
        for item in data.get("risks", []):
            risk = self._to_draft(item, batch, offset, raw)
            if risk is not None:
                risks.append(risk)
        return risks, meta.json_retry_count

    def _build_user_prompt(self, batch: list[ClauseDraft]) -> str:
        """构造送审提示词。

        给每条条款编号，便于模型在输出里通过序号指认来源条款
        （虽然 schema 里用 quote 定位，序号仍有助于模型聚焦）。
        """
        lines = ["请审查以下合同条款，识别其中的法律风险：", ""]
        for i, c in enumerate(batch):
            no = c.clause_no or f"#{c.seq}"
            title = f" {c.title}" if c.title else ""
            lines.append(f"[条款 {i}] {no}{title}")
            lines.append(c.content)
            lines.append("")
        lines.append("请按指定 JSON 格式输出审查结果。")
        return "\n".join(lines)

    def _to_draft(
        self,
        item: Any,
        batch: list[ClauseDraft],
        offset: int,
        raw: str,
    ) -> LlmRiskDraft | None:
        """把模型输出的一条风险转成 draft，并做字段级容错。"""
        if not isinstance(item, dict):
            logger.warning("LLM 返回的 risks 元素不是对象，已跳过: %r", item)
            return None

        title = str(item.get("title") or "").strip()
        if not title:
            logger.warning("LLM 返回的风险缺少 title，已跳过: %r", item)
            return None

        level = _parse_level(item.get("risk_level"))
        category = _parse_category(item.get("category"))
        quote = (item.get("quote") or "").strip() or None

        # 用 quote 反查它来自哪条条款，便于挂 clause_id
        clause_index = _find_clause_index(quote, batch, offset)

        legal_basis = (item.get("legal_basis") or "").strip() or None

        return LlmRiskDraft(
            title=title[:255],
            risk_level=level,
            category=category,
            reason=str(item.get("reason") or title)[:4000],
            suggestion=(str(item["suggestion"])[:4000] if item.get("suggestion") else None),
            legal_basis=legal_basis,
            quote=quote,
            clause_index=clause_index,
            raw_snippet=raw[:4000],
            # 法条是否可信由 `mark_legal_basis_review` 在拿到知识库后判定，
            # 这里先按"有法条即需复核"的保守策略标记
            need_review=bool(legal_basis),
        )


def _validate_shape(data: Any) -> dict:
    """校验 LLM 输出的顶层结构。

    只做**最小必要校验**：必须是含 `risks` 列表的对象。
    字段级的容错交给 `_to_draft`，避免一条脏数据让整批作废。
    """
    if not isinstance(data, dict):
        raise ValueError(f"顶层应为对象，实际是 {type(data).__name__}")
    if "risks" not in data:
        raise ValueError("缺少 risks 字段")
    if not isinstance(data["risks"], list):
        raise ValueError(f"risks 应为数组，实际是 {type(data['risks']).__name__}")
    return data


def _parse_level(raw: Any) -> RiskLevel:
    """解析风险等级，无法识别时保守取 medium。

    取 medium 而非 low：漏报比误报代价大（§8.3 的取高原则），
    无法识别等级时不应假设"没问题"。
    """
    text = str(raw or "").strip().lower()
    for level in RiskLevel:
        if level.value == text:
            return level
    logger.warning("无法识别的 risk_level %r，保守取 medium", raw)
    return RiskLevel.MEDIUM


def _parse_category(raw: Any) -> RiskCategory:
    text = str(raw or "").strip().lower()
    for cat in RiskCategory:
        if cat.value == text:
            return cat
    return RiskCategory.OTHER


def _find_clause_index(
    quote: str | None, batch: list[ClauseDraft], offset: int
) -> int | None:
    """按引用反查条款下标（返回全局下标）。

    用**去空白比较**：模型常把原文的换行抹掉，直接 `in` 判断会失败。
    """
    if not quote:
        return None
    from app.services.parsing.text_normalize import strip_whitespace

    compact_quote = strip_whitespace(quote)
    if not compact_quote:
        return None
    for i, clause in enumerate(batch):
        if compact_quote in strip_whitespace(clause.content):
            return offset + i
    # 退一步：用引用的前 12 字做前缀匹配
    head = compact_quote[:12]
    for i, clause in enumerate(batch):
        if head and head in strip_whitespace(clause.content):
            return offset + i
    return None
