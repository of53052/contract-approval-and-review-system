"""解析层的中间数据结构。

设计依据：docs/architecture.md §6.2（统一锚点模型）、§6.3（坐标映射）。

这一层的核心约定：**全部坐标统一为 PDF point**，与页面尺寸同单位。
OCR 路径在进入本层之前就已完成像素→point 换算（× 72/DPI），
因此后续所有代码（锚点构建、前端渲染）都无需关心数据来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    """解析出的块类型。用于区分正文段落与表格，表格内容不参与条款切分。"""

    PARAGRAPH = "paragraph"
    TABLE = "table"
    TITLE = "title"


@dataclass(frozen=True, slots=True)
class CharBox:
    """单个字符及其坐标。

    这是"字符级 bbox"能力的最小单元：有了它，任意字符区间都能算出
    精确的包围盒，从而支持"高亮到具体几个字"而非整段。
    """

    char: str
    x0: float
    y0: float
    x1: float
    y1: float
    page_no: int

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class PageText:
    """单页的文本与字符坐标。

    `text` 与 `char_boxes` **长度严格相等、下标一一对应**，
    这是全文偏移量（`char_start` / `char_end`）能落到具体字符上的前提。
    """

    page_no: int
    width: float
    height: float
    text: str
    char_boxes: list[CharBox]
    source: str  # AnchorSource 的值：native_text / ocr

    def __post_init__(self) -> None:
        if len(self.text) != len(self.char_boxes):
            raise ValueError(
                f"第 {self.page_no} 页 text 长度 {len(self.text)} 与 "
                f"char_boxes 长度 {len(self.char_boxes)} 不一致，"
                "全文偏移量将无法映射到坐标"
            )

    def bbox_for_range(self, start: int, end: int) -> tuple[float, float, float, float] | None:
        """取 [start, end) 区间内所有字符的并集包围盒（页内局部下标）。

        返回 None 表示区间为空或全部为空白字符。
        """
        if start >= end or start < 0:
            return None
        boxes = [b for b in self.char_boxes[start:end] if not b.char.isspace()]
        if not boxes:
            return None
        return (
            min(b.x0 for b in boxes),
            min(b.y0 for b in boxes),
            max(b.x1 for b in boxes),
            max(b.y1 for b in boxes),
        )


@dataclass
class ParsedBlock:
    """解析出的一个内容块，用于后续条款切分。"""

    kind: BlockKind
    text: str
    page_no: int
    page_end: int
    para_index: int
    char_start: int | None
    char_end: int | None
    bbox: tuple[float, float, float, float] | None


@dataclass
class DocumentText:
    """整份文档的文本层，是锚点对齐的唯一数据源。"""

    pages: list[PageText]
    blocks: list[ParsedBlock] = field(default_factory=list)
    # 各页在全文中的起始偏移，由 __post_init__ 计算
    _page_offsets: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._rebuild_offsets()

    def _rebuild_offsets(self) -> None:
        """重算各页在全文中的起始偏移。

        全文 = 各页文本用 "\n" 连接，因此第 n 页的偏移 = 前面各页长度之和 + 页数。
        """
        offsets: list[int] = []
        cursor = 0
        for idx, page in enumerate(self.pages):
            offsets.append(cursor)
            cursor += len(page.text)
            if idx < len(self.pages) - 1:
                cursor += 1  # 页间分隔符
        self._page_offsets = offsets

    @property
    def full_text(self) -> str:
        """全文。`char_start` / `char_end` 均以此为基准。"""
        return "\n".join(p.text for p in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, page_no: int) -> PageText:
        """按页码（从 1 开始）取页。"""
        if not 1 <= page_no <= len(self.pages):
            raise IndexError(f"页码 {page_no} 超出范围 [1, {len(self.pages)}]")
        return self.pages[page_no - 1]

    def page_offset(self, page_no: int) -> int:
        """第 page_no 页在全文中的起始偏移。"""
        return self._page_offsets[page_no - 1]

    def locate(self, char_start: int, char_end: int) -> tuple[int, int, int]:
        """把全文偏移映射为 (页码, 页内起始, 页内结束)。

        用于"已知全文区间，求它在哪一页的哪几个字符"。
        """
        for idx, page in enumerate(self.pages):
            base = self._page_offsets[idx]
            page_end_offset = base + len(page.text)
            if char_start < page_end_offset or idx == len(self.pages) - 1:
                local_start = max(0, char_start - base)
                local_end = min(len(page.text), char_end - base)
                return page.page_no, local_start, local_end
        raise ValueError(f"无法定位字符区间 [{char_start}, {char_end})")
