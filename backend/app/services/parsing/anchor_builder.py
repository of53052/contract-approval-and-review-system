"""锚点构建与引用对齐。

设计依据：docs/architecture.md §6.4（三级降级）、docs/data-model.md §5.7。

**本模块是坐标映射的唯一实现点**。任何需要"文本 → 页面坐标"的代码
都必须走这里，避免坐标逻辑散落导致不一致。

三级降级策略：
    ① 精确匹配（原文 / 去空白）
    ② 模糊匹配（归一化 + 相似度阈值）
    ③ 降级到段落级锚点（标记 anchor_level=paragraph）

**核心原则**：引用无法定位时必须显式标记 `unanchored`，绝不静默丢弃，
也不伪造位置。这是验收标准"原文与风险卡片双向高亮跳转"的兜底。

实现要点：归一化会改变字符数（全角→半角、省略号折叠），因此本模块
**始终维护归一化下标 → 原文下标的映射表**，保证任何匹配结果都能
准确还原到原文位置，而不是靠比例估算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.enums import AnchorLevel, AnchorSource
from app.services.parsing.text_normalize import normalize, similarity, strip_whitespace
from app.services.parsing.types import DocumentText

logger = logging.getLogger(__name__)

#: 模糊匹配的相似度下限。低于此值视为未命中，继续降级。
#: 取 0.72：实测"标点差异 + 省略中段"的引用相似度约 0.8，
#: 而无关文本通常在 0.4 以下，阈值有较宽的安全间隔。
FUZZY_THRESHOLD = 0.72

#: 模糊匹配时允许的对齐偏移（归一化字符数）。
#: 取 8：seed 命中位置可能有几个字的偏差（引用开头被改写），
#: 在此范围内各取等长窗口比较，取相似度最高者。
#:
#: ⚠️ **不要用"大窗口 + 固定相似度"**：`SequenceMatcher.ratio` 是
#: `2M/T`，窗口比目标长 N 倍时比值会被稀释到 1/N。
#: 实测窗口取 4 倍长度时，完全匹配的引用得分只有 0.39，必然漏检。
FUZZY_ALIGN_TOLERANCE = 8

#: 用于缩小模糊搜索范围的 seed 长度
_FUZZY_SEED_LEN = 6

#: 模糊匹配最多考察的候选位置数，防止常见词导致性能塌陷
_FUZZY_MAX_CANDIDATES = 20

#: 引用过短时不做模糊匹配（误报率高）
_FUZZY_MIN_LEN = 4


@dataclass
class AnchorResult:
    """锚点构建结果。

    `level = NONE` 时 `page_no` / `bbox` 为 None，调用方应据此把
    `risk_item.unanchored` 置 1，而**不写入 anchor 表**（见 §5.7 不变量）。
    """

    level: AnchorLevel
    page_no: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote_text: str | None = None
    source: AnchorSource = AnchorSource.NATIVE_TEXT
    confidence: float | None = None
    #: 仅部分命中：引用被 LLM 省略/改写，只有其中一段能在原文定位。
    #: 前端据此提示"部分匹配"，避免用户误以为整句都被高亮。
    partial: bool = False

    @property
    def anchored(self) -> bool:
        return self.level != AnchorLevel.NONE and self.page_no is not None


class AnchorBuilder:
    """基于 `DocumentText` 的引用对齐器。

    构造时预建归一化索引，避免每次查询都重新归一化全文
    （一次审查可能产生十几个引用，重复归一化是明显浪费）。
    """

    def __init__(self, doc: DocumentText) -> None:
        self.doc = doc
        self._full_text = doc.full_text
        self._page_offsets = self._compute_page_offsets(doc)
        # 归一化文本 + 下标映射：norm_map[i] = 归一化第 i 个字符在原文中的下标
        self._norm_text, self._norm_map = self._build_norm_index(self._full_text)
        # 去空白文本 + 下标映射，用于跨行引用
        self._compact_text, self._compact_map = self._build_compact_index(self._full_text)

    # ---------------- 公开入口 ----------------

    def locate(
        self,
        quote: str,
        *,
        source: AnchorSource = AnchorSource.NATIVE_TEXT,
        confidence: float | None = None,
    ) -> AnchorResult:
        """把一段引用文本对齐到原文位置。

        依次尝试精确匹配 → 模糊匹配 → 段落级降级；全部失败返回 NONE。
        """
        if not quote or not quote.strip():
            return AnchorResult(level=AnchorLevel.NONE, source=source)

        # 顺序即优先级：越靠前的结果越精确。
        # 片段匹配排在模糊匹配之前，因为它能给出精确字符区间；
        # 模糊匹配是最后的精度兜底。
        for finder in (
            self._match_raw,
            self._match_compact,
            self._match_fragment,
            self._match_fuzzy,
        ):
            result = finder(quote, source, confidence)
            if result is not None:
                return result

        logger.info("引用无法定位，标记 unanchored: %r", quote[:40])
        return AnchorResult(level=AnchorLevel.NONE, quote_text=quote[:512], source=source)

    def locate_block(self, page_no: int, para_index: int) -> AnchorResult:
        """按块定位（段落级锚点）。

        用于两种场景：
        - 规则引擎按段落判定命中，只需定位到段
        - DOCX 降级为 passthrough（无分页）时的兜底定位
        """
        for blk in self.doc.blocks:
            if blk.page_no == page_no and blk.para_index == para_index:
                if blk.bbox is None:
                    break
                return AnchorResult(
                    level=AnchorLevel.PARAGRAPH,
                    page_no=page_no,
                    bbox=blk.bbox,
                    char_start=None,  # 段落级按设计不带字符区间
                    char_end=None,
                    quote_text=blk.text[:512],
                    source=AnchorSource.NATIVE_TEXT,
                )
        return AnchorResult(level=AnchorLevel.NONE, source=AnchorSource.NATIVE_TEXT)

    # ---------------- 匹配实现 ----------------

    def _match_raw(
        self, quote: str, source: AnchorSource, confidence: float | None
    ) -> AnchorResult | None:
        """①-a 原文直接查找。"""
        pos = self._full_text.find(quote.strip())
        if pos < 0:
            return None
        return self._from_real_range(pos, pos + len(quote.strip()), quote, AnchorLevel.EXACT,
                                     source, confidence)

    def _match_compact(
        self, quote: str, source: AnchorSource, confidence: float | None
    ) -> AnchorResult | None:
        """①-b 去空白后查找，应对"引用跨行但原文含换行"的情况。"""
        target = strip_whitespace(quote)
        if not target:
            return None
        pos = self._compact_text.find(target)
        if pos < 0:
            return None
        real_start = self._compact_map[pos]
        real_end = self._compact_map[pos + len(target) - 1] + 1
        return self._from_real_range(real_start, real_end, quote, AnchorLevel.EXACT,
                                     source, confidence)

    def _match_fragment(
        self, quote: str, source: AnchorSource, confidence: float | None
    ) -> AnchorResult | None:
        """①-c 片段锚定：按标点把引用切成片段，锚定其中最长的可定位片段。

        **为什么需要这一层**：LLM 引用时常"省略中段"，例如原文是
        「乙方逾期交货的，应承担甲方全部损失，赔偿责任无上限。」而 LLM 返回
        「乙方逾期交货的，赔偿责任无上限。」——中间 10 个字被略去。

        此时整段相似度只有 0.44，模糊匹配必然漏检；但引用的**首尾片段**
        都是原文的连续子串，锚定其中最长的一段，就能给出精确高亮位置。

        语义上是诚实的：定位到"引用中确实存在于原文的那部分"，
        而不是估算一个可能偏移的位置。
        """
        fragments = _split_fragments(quote)
        if not fragments:
            return None
        # 长片段优先：它更可能唯一、更具代表性
        for frag in sorted(fragments, key=len, reverse=True):
            for finder in (self._match_raw, self._match_compact, self._match_fuzzy):
                result = finder(frag, source, confidence)
                if result is not None:
                    logger.info(
                        "引用经片段锚定成功（片段 %r）: %r", frag[:20], quote[:30]
                    )
                    # quote_text 保留**真实命中的片段**（它是校验与展示的依据），
                    # 另用 partial 标记"整句未被完整锚定"。
                    # 不要把 quote_text 换成完整引用——那样会掩盖实际定位范围。
                    result.partial = True
                    return result
        return None

    def _match_fuzzy(
        self, quote: str, source: AnchorSource, confidence: float | None
    ) -> AnchorResult | None:
        """② 模糊匹配：归一化后在候选位置做滑动窗口相似度比较。

        命中后仍给出**精确的字符区间与 bbox**——因为映射表保证了
        归一化下标能准确还原到原文位置，无需退化成段落级。
        """
        target = normalize(quote)
        if len(target) < _FUZZY_MIN_LEN:
            return None

        seed = target[:_FUZZY_SEED_LEN]
        candidates: list[int] = []
        start = 0
        while len(candidates) < _FUZZY_MAX_CANDIDATES:
            idx = self._norm_text.find(seed, start)
            if idx < 0:
                break
            candidates.append(idx)
            start = idx + 1

        # 在 seed 命中位置附近做对齐微调，窗口长度**始终等于目标长度**，
        # 保证 ratio 不被稀释（见 FUZZY_ALIGN_TOLERANCE 的说明）。
        tlen = len(target)
        best_score = 0.0
        best_start = -1
        for cand in candidates:
            for offset in range(-FUZZY_ALIGN_TOLERANCE, FUZZY_ALIGN_TOLERANCE + 1):
                start = cand + offset
                if start < 0:
                    continue
                end = start + tlen
                if end > len(self._norm_text):
                    break
                score = similarity(target, self._norm_text[start:end])
                if score > best_score:
                    best_score = score
                    best_start = start

        if best_start < 0 or best_score < FUZZY_THRESHOLD:
            return None

        norm_start = best_start
        norm_end = best_start + tlen
        real_start = self._norm_map[norm_start]
        real_end = self._norm_map[norm_end - 1] + 1
        logger.info(
            "引用模糊匹配成功（相似度 %.3f）: %r", best_score, quote[:30]
        )
        return self._from_real_range(real_start, real_end, quote, AnchorLevel.FUZZY,
                                     source, confidence)

    # ---------------- 结果构造 ----------------

    def _from_real_range(
        self,
        char_start: int,
        char_end: int,
        quote: str,
        level: AnchorLevel,
        source: AnchorSource,
        confidence: float | None,
    ) -> AnchorResult | None:
        """把原文区间转成 (页码, bbox)。"""
        if char_start >= char_end:
            return None
        page_no = self._page_of(char_start)
        if page_no is None:
            return None
        page = self.doc.page(page_no)
        base = self._page_offsets[page_no - 1]
        local_start = max(0, char_start - base)
        local_end = min(len(page.text), char_end - base)
        bbox = page.bbox_for_range(local_start, local_end)
        if bbox is None:
            # 区间内全是空白（如引用恰好落在换行处），无法给出有效框
            return None
        return AnchorResult(
            level=level,
            page_no=page_no,
            bbox=bbox,
            char_start=char_start,
            char_end=char_end,
            quote_text=self._full_text[char_start:char_end][:512] or quote[:512],
            source=source,
            confidence=confidence,
        )

    def _page_of(self, char_pos: int) -> int | None:
        """按原文偏移求所属页码（从 1 开始）。"""
        for idx, page in enumerate(self.doc.pages):
            base = self._page_offsets[idx]
            if char_pos < base + len(page.text):
                return page.page_no
        # 落在末页之后的换行符上，归到末页
        return self.doc.pages[-1].page_no if self.doc.pages else None

    # ---------------- 索引构建 ----------------

    @staticmethod
    def _compute_page_offsets(doc: DocumentText) -> list[int]:
        """各页在全文中的起始偏移（与 DocumentText 内部算法保持一致）。"""
        offsets: list[int] = []
        cursor = 0
        for idx, page in enumerate(doc.pages):
            offsets.append(cursor)
            cursor += len(page.text)
            if idx < len(doc.pages) - 1:
                cursor += 1
        return offsets

    @staticmethod
    def _build_norm_index(text: str) -> tuple[str, list[int]]:
        """逐字符归一化，同时记录每个归一化字符对应的原文下标。

        逐字符做是为了保住映射关系；整体调用 `normalize()` 会丢失对应性。
        代价是标点映射表外的字符走原样保留，与 `normalize` 的语义一致。
        """
        from app.services.parsing.text_normalize import _PUNCT_MAP, to_halfwidth

        chars: list[str] = []
        mapping: list[int] = []
        pending_space = False
        for i, ch in enumerate(text):
            # 空白折叠为单个空格
            if ch.isspace():
                pending_space = True
                continue
            if pending_space and chars:
                chars.append(" ")
                mapping.append(i)
                pending_space = False
            h = to_halfwidth(ch)
            chars.append(_PUNCT_MAP.get(h, h))
            mapping.append(i)
        return "".join(chars), mapping

    @staticmethod
    def _build_compact_index(text: str) -> tuple[str, list[int]]:
        """去空白文本 + 映射表。"""
        chars: list[str] = []
        mapping: list[int] = []
        for i, ch in enumerate(text):
            if ch.isspace():
                continue
            chars.append(ch)
            mapping.append(i)
        return "".join(chars), mapping


#: 片段切分的最小长度。短于此长度的片段信息量不足，容易误命中。
_MIN_FRAGMENT_LEN = 4

#: 用于切分引用的标点（含中英文）
_FRAGMENT_SEPARATORS = "，。；：、！？,.;:!?\n\r\t 　（）()【】[]《》<>“”\"'"


def _split_fragments(quote: str) -> list[str]:
    """把引用按标点切成有意义的片段，过滤掉过短的。

    只做切分与过滤，不做归一化——归一化交给下游的各个 matcher，
    这样片段匹配能复用精确匹配的去空白能力。
    """
    parts: list[str] = []
    buf: list[str] = []
    for ch in quote:
        if ch in _FRAGMENT_SEPARATORS:
            if buf:
                parts.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in parts if len(p) >= _MIN_FRAGMENT_LEN]
