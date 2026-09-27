"""LLM JSON 输出提取与校验。

设计依据：docs/architecture.md §9.4（三层兜底）。

**为什么必须有这一层（实测结论）**：

项目实际使用的端点接受 `response_format={"type":"json_object"}` 参数，
但**该参数无效**——实测 3/3 次都返回 markdown 包裹 + 尾部解释文字：

    ```json
    {"level":"high","title":"无限赔偿责任条款"}
    ```

    理由：未设赔偿上限，责任敞口不可预估...

因此不能假设"用了 json_object 就一定拿到纯 JSON"，
必须实现「提取 + 校验 + 重试」三层兜底。

**另一个实测事实**：响应含 `reasoning_content` 字段（推理模型的思维链），
与 `content` 分开返回，解析时必须正确忽略，否则会把思维链当成 JSON 去解析。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: 匹配 ```json ... ``` 或 ``` ... ``` 代码块
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

#: 匹配最外层花括号或方括号包裹的内容（非贪婪，优先取最短完整结构）
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)
_BRACKET_RE = re.compile(r"\[.*\]", re.DOTALL)


class JsonExtractionError(ValueError):
    """无法从文本中提取出 JSON。"""


def extract_json(text: str) -> Any:
    """从 LLM 输出中提取 JSON 对象/数组。

    按可靠性从高到低尝试四种策略，任一成功即返回：
    1. 整体就是合法 JSON（理想情况）
    2. ``` 代码块内的内容
    3. 首个平衡的 `{...}` 或 `[...]` 结构（用括号计数，不用正则贪婪匹配）
    4. 去除尾部解释文字后重试

    抛 `JsonExtractionError` 表示全部失败。
    """
    if not text or not text.strip():
        raise JsonExtractionError("LLM 输出为空")

    candidate = text.strip()

    # 策略 1：整体可解析
    parsed = _try_parse(candidate)
    if parsed is not None:
        return parsed

    # 策略 2：代码块
    for m in _FENCE_RE.finditer(text):
        parsed = _try_parse(m.group(1).strip())
        if parsed is not None:
            logger.debug("JSON 从代码块提取成功")
            return parsed

    # 策略 3：括号平衡扫描（比正则可靠：正则无法处理嵌套与字符串内的括号）
    parsed = _extract_balanced(candidate)
    if parsed is not None:
        logger.debug("JSON 通过括号平衡扫描提取成功")
        return parsed

    # 策略 4：逐行截断重试（应对"JSON 后跟解释文字但无代码块"）
    lines = candidate.splitlines()
    for end in range(len(lines), 0, -1):
        parsed = _try_parse("\n".join(lines[:end]).strip())
        if parsed is not None:
            logger.debug("JSON 通过截断尾部文字提取成功（保留 %s 行）", end)
            return parsed

    raise JsonExtractionError(
        f"无法从输出中提取 JSON（长度 {len(text)}）：{text[:200]!r}"
    )


def _try_parse(text: str) -> Any | None:
    """尝试解析，失败返回 None（不抛异常）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_balanced(text: str) -> Any | None:
    """扫描首个括号平衡的片段并解析。

    正确处理字符串字面量与转义，因此 `{"a": "}"}` 这类内容不会被误截断。
    """
    # 按"谁先出现"决定尝试顺序：若固定先试 `{`，则
    # `结果：[{"t":1},{"t":2}]` 会被截成 `{"t":1}`（实测发现的缺陷）。
    openers = []
    for opener, closer in (("{", "}"), ("[", "]")):
        pos = text.find(opener)
        if pos >= 0:
            openers.append((pos, opener, closer))
    openers.sort()

    for _, opener, closer in openers:
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    parsed = _try_parse(text[start : i + 1])
                    if parsed is not None:
                        return parsed
                    break
    return None


def build_retry_hint(error: str, previous: str) -> str:
    """构造 JSON 重试时的反馈消息。

    把**具体错误**回喂给模型，比单纯重试有效得多——实测直接重试
    常得到同样的错误格式，带上错误信息后模型会主动修正。
    """
    preview = previous[:500]
    return (
        "你上一次的输出无法解析为 JSON，错误信息如下：\n"
        f"{error}\n\n"
        "上一次输出的开头是：\n"
        f"{preview}\n\n"
        "请**只输出一个合法的 JSON 对象**，不要包含 markdown 代码块标记、"
        "不要包含任何解释性文字、不要在 JSON 前后添加任何内容。"
    )
