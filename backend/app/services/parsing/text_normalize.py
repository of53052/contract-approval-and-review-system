"""文本归一化工具。

用途有两个，都必须**只依赖标准库**：
1. 引用对齐：LLM 返回的原文引用常改标点、省略号、全半角混用（架构文档 §6.4）
2. 规则匹配：关键词命中前先归一化，避免"全角括号 vs 半角括号"这类假阴性

设计原则：归一化**只用于匹配**，绝不回写展示文本——用户看到的必须永远是原文。
"""

from __future__ import annotations

import re
import unicodedata

# 中文标点 -> 统一形式。只映射"同一标点的全角/半角变体"，
# 不做"句号换逗号"这类语义改写。
_PUNCT_MAP = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "－": "-", "～": "~", "·": ".", "　": " ",
}

# 空白类字符（含全角空格、不换行空格）统一折叠为单个半角空格
_WHITESPACE_RE = re.compile(r"[\s\u00a0\u3000]+")

# 省略号的各种写法
_ELLIPSIS_RE = re.compile(r"\.{2,}|。{2,}|·{3,}|…+")


def to_halfwidth(text: str) -> str:
    """全角字符转半角（NFKC 的窄化版本）。

    只处理 ASCII 可见区对应的全角形式（FF01-FF5E）与全角空格（3000），
    不用 `unicodedata.normalize("NFKC")` 全量归一化——NFKC 会把
    "①"→"1"、"㎡"→"m2" 这类**语义改写**也做掉，对法律文本是危险的。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str, *, keep_punct: bool = True) -> str:
    """归一化文本用于**匹配**。

    步骤：全角转半角 -> 标点统一 -> 省略号折叠 -> 空白折叠 -> 去首尾空白。

    `keep_punct=False` 时进一步去掉全部标点与空白，用于"只比对字词"的场景
    （如 LLM 引用漏了标点）。
    """
    if not text:
        return ""
    s = to_halfwidth(unicodedata.normalize("NFC", text))
    s = _ELLIPSIS_RE.sub("...", s)
    if keep_punct:
        s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    else:
        s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("P")
                    and not ch.isspace())
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip()


def strip_whitespace(text: str) -> str:
    """去掉所有空白。中文文本里空格往往是排版噪声，比对时可忽略。"""
    return _WHITESPACE_RE.sub("", text)


def similarity(a: str, b: str) -> float:
    """相似度 [0,1]，基于 difflib 的最长匹配比。

    选 difflib 而非第三方库：标准库够用，且不引入新依赖（AGENTS 约束）。
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()
