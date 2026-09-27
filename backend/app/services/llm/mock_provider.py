"""Mock Provider：无 Key 时也能跑通完整链路。

设计依据：docs/architecture.md §9.3。

**职责**：回放预置的审查结果，带完整依据链。
不是"返回空数据"，而是**真实可用的降级方案**——演示环境没有 Key 时，
整条链路（解析 → 审查 → 落库 → 报告 → 回写）必须仍然走通。

识别方式：按提示词中的关键词匹配预置结论；无匹配时返回一条
保守的"无法判定"结果，**绝不编造具体风险**（防幻觉原则）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.services.llm.base import ChatMessage, ChatResult, LlmJsonError, LlmUsage

logger = logging.getLogger(__name__)


#: 预置的条款审查结果。
#: 键是触发关键词，值是要回放的 JSON。按架构文档 §8.2 的职责边界设计：
#: LLM 负责"权责是否对等、表述是否高危"这类语义判断。
FIXTURES: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    (
        ("无上限", "全部损失", "一切损失", "不受限制"),
        {
            "risks": [{
                "title": "赔偿责任无上限",
                "risk_level": "high",
                "category": "liability",
                "reason": "违约责任未设赔偿上限，我方责任敞口不可预估，"
                          "可能因单次违约承担远超合同金额的损失。",
                "legal_basis": "《中华人民共和国民法典》第五百八十五条",
                "suggestion": "建议增加责任上限条款：除故意或重大过失外，"
                              "任一方承担的赔偿责任总额不超过合同总金额。",
                "quote": "应承担甲方全部损失，赔偿责任无上限",
            }],
        },
    ),
    (
        ("知识产权归供应商", "知识产权归乙方", "知识产权归对方", "所有权归供应商"),
        {
            "risks": [{
                "title": "知识产权归属供应商",
                "risk_level": "high",
                "category": "intellectual_property",
                "reason": "知识产权全部归对方所有，我方将丧失项目成果的"
                          "所有权，后续使用、修改、迁移均受制于人。",
                "legal_basis": "《中华人民共和国民法典》第八百四十三条",
                "suggestion": "建议约定：本项目专门产生的成果知识产权归我方所有，"
                              "对方既有知识产权仍归其所有并授予我方必要许可。",
                "quote": "本项目产生的知识产权归供应商所有",
            }],
        },
    ),
    (
        ("到货即付", "收货后即付", "到货后全额", "验收前付清", "预付全款"),
        {
            "risks": [{
                "title": "到货即付全款无验收条款",
                "risk_level": "high",
                "category": "acceptance",
                "reason": "付款义务不以验收合格为前提，若交付物存在质量缺陷，"
                          "我方已付款将丧失主要履约抗辩手段。",
                "legal_basis": "《中华人民共和国民法典》第六百二十六条",
                "suggestion": "建议改为：验收合格并出具书面验收单后，"
                              "方支付相应比例款项；保留不低于 10% 的质保金。",
                "quote": "到货即付全款",
            }],
        },
    ),
    (
        ("境外仲裁", "香港仲裁", "新加坡仲裁", "对方所在地法院", "供应商所在地法院"),
        {
            "risks": [{
                "title": "管辖地约定不利于我方",
                "risk_level": "high",
                "category": "jurisdiction",
                "reason": "约定境外或对方所在地管辖，将显著提高我方维权成本，"
                          "并可能面临不熟悉的法律程序。",
                "legal_basis": "《中华人民共和国民事诉讼法》第三十五条",
                "suggestion": "建议改为由我方所在地有管辖权的人民法院管辖。",
                "quote": "提交香港仲裁",
            }],
        },
    ),
    (
        ("保密期限", "保密义务"),
        {
            "risks": [{
                "title": "保密义务期限不明确",
                "risk_level": "medium",
                "category": "confidentiality",
                "reason": "未明确保密期限，义务边界模糊，"
                          "离职或合同终止后是否仍需保密存在争议空间。",
                "legal_basis": "《中华人民共和国民法典》第五百零一条",
                "suggestion": "建议约定：保密义务自签署之日起持续至该信息"
                              "进入公有领域为止，且不少于 3 年。",
                "quote": "负有保密义务",
            }],
        },
    ),
    (
        ("不可抗力",),
        {
            "risks": [{
                "title": "不可抗力通知时效缺失",
                "risk_level": "medium",
                "category": "force_majeure",
                "reason": "未约定通知时效与证明文件要求，"
                          "事后主张不可抗力免责时举证困难。",
                "legal_basis": "《中华人民共和国民法典》第五百九十条",
                "suggestion": "建议约定：受影响方应在事件发生后 15 日内书面通知，"
                              "并在 30 日内提供有权机关证明文件。",
                "quote": "不可抗力",
            }],
        },
    ),
    (
        ("违约金", "百分之二十五", "25%"),
        {
            "risks": [{
                "title": "违约金比例偏高",
                "risk_level": "medium",
                "category": "liability",
                "reason": "违约金比例超出参考上限（20%），"
                          "且若为单向约定则责任分配不对等。",
                "legal_basis": "《中华人民共和国民法典》第五百八十五条",
                "suggestion": "建议将违约金比例下调至合同总金额的 20% 以内，"
                              "并双向对等约定。",
                "quote": "违约金",
            }],
        },
    ),
]

#: 无任何关键词命中时的保守返回。
#: 刻意返回空风险列表而非编造内容——防幻觉原则要求"宁可漏报也不伪造"。
NO_MATCH_RESULT: dict[str, Any] = {
    "risks": [],
    "note": "MockProvider 未匹配到预置结论；这是模拟输出，不代表真实审查意见。",
}


class MockProvider:
    """回放预置结果的 Provider。"""

    name = "mock"

    def __init__(self, *, latency_ms: int = 50) -> None:
        #: 模拟一点延迟，让前端进度条行为与真实调用一致（便于联调）
        self.latency_ms = latency_ms
        self.calls: list[list[ChatMessage]] = []

    def is_available(self) -> bool:
        return True

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        """返回文本形式的预置结果（把 JSON 序列化）。"""
        import json

        data = self._match(messages)
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        content = json.dumps(data, ensure_ascii=False)
        self.calls.append(list(messages))
        return ChatResult(
            content=content,
            usage=LlmUsage(prompt_tokens=0, completion_tokens=0),
            raw=content,
        )

    def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        validator: Any = None,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> tuple[Any, ChatResult]:
        """返回结构化预置结果。

        Mock 的输出天然合法，因此不做重试；但仍走 validator，
        保证"Mock 与真实 Provider 的行为一致"——若 schema 不匹配，
        在 Mock 下就能暴露，不必等到接真实端点。
        """
        data = self._match(messages)
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        self.calls.append(list(messages))

        if validator is not None:
            try:
                data = validator(data)
            except Exception as exc:  # noqa: BLE001
                # Mock 数据不匹配 schema 是**开发期 bug**，必须显式暴露
                raise LlmJsonError(
                    f"MockProvider 预置数据不符合 schema: {type(exc).__name__}: {exc}",
                    raw=str(data)[:500],
                    retry_count=0,
                ) from exc

        result = ChatResult(
            content=str(data),
            usage=LlmUsage(prompt_tokens=0, completion_tokens=0),
            json_retry_count=0,
            raw=str(data),
        )
        return data, result

    # ---------------- 内部 ----------------

    def _match(self, messages: list[ChatMessage]) -> dict[str, Any]:
        """按关键词匹配预置结论，**命中多组时全部合并**。

        最初的实现是"命中第一组就返回"，这会让 Mock 在同一批含多个风险的
        条款里永远只回一条——演示时看起来像漏报，也走不到 `Merger` 的
        多风险合并、取高升级与双来源留痕路径（真实 LLM 会一次给出多条）。

        去重按 `title`：不同 fixture 的关键词可能同时命中（如某条款同时含
        「无上限」与「违约金」），但它们指向的是同一条风险。
        """
        blob = "\n".join(m.content for m in messages)
        risks: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        matched: list[str] = []

        for keywords, payload in FIXTURES:
            if not any(kw in blob for kw in keywords):
                continue
            matched.append(keywords[0])
            for risk in payload.get("risks", []):
                title = str(risk.get("title") or "")
                if title and title in seen_titles:
                    continue
                seen_titles.add(title)
                risks.append(risk)

        if not risks:
            logger.info("MockProvider 未命中任何预置结论，返回保守空结果")
            return NO_MATCH_RESULT

        logger.info(
            "MockProvider 命中 %s 组预置结论（%s），合并出 %s 条风险",
            len(matched), "、".join(matched), len(risks),
        )
        return {"risks": risks}
