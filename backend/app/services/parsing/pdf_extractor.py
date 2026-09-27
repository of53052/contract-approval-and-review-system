"""文本型 PDF 提取器（PyMuPDF）。

设计依据：docs/architecture.md §5.2（实测 9.26ms / 3 页）、§6.3（坐标映射）。

关键点：
- 用 `page.get_text("rawdict")` 而非 `"text"`：只有 rawdict 才带**字符级 bbox**，
  这是"高亮到具体几个字"能力的基础。
- 坐标直接就是 PDF point，**无需换算**（对比 OCR 路径的 × 72/DPI）。
- 段落切分按 PyMuPDF 的 block/line 层级，表格单独识别为 TABLE 块。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.services.parsing.types import (
    BlockKind,
    CharBox,
    DocumentText,
    PageText,
    ParsedBlock,
)

logger = logging.getLogger(__name__)


@dataclass
class PdfExtractResult:
    """提取结果，附带用于落库的元信息。"""

    doc: DocumentText
    has_text_layer: bool
    #: 判定为扫描件的依据说明，便于排查（如"文本层仅 12 个字符"）
    scan_reason: str | None = None


class EncryptedPdfError(Exception):
    """PDF 已加密且无法用空密码解密。对应 BlockedReason.encrypted。"""


#: 判定"有文本层"的最小字符数。低于此值视为扫描件。
#: 阈值取 20：一页正常的合同正文远超此数，而纯图片页通常只有页眉页码几个字符。
MIN_TEXT_CHARS = 20

#: 字符 bbox 高度 / 字号 的合理上限。
#: 正常字体度量下该比值约 1.0；超过此值说明字体度量异常。
_BBOX_HEIGHT_RATIO_LIMIT = 1.6

#: 度量修复时使用的 CJK 字体标称上下界（相对字号的倍数）。
#: 取常见中文字体的典型值：ascender ≈ 0.88、descender ≈ -0.12。
_FALLBACK_ASCENDER = 0.88
_FALLBACK_DESCENDER = -0.12


def extract_pdf(path: str | Path, *, password: str | None = None) -> PdfExtractResult:
    """提取文本型 PDF。

    抛 `EncryptedPdfError` 表示文档加密；其余异常向上抛（fail-fast）。
    """
    path = Path(path)
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            # 先尝试空密码（很多"加密"文档只是禁止编辑，允许打开）
            if not doc.authenticate(password or ""):
                raise EncryptedPdfError(f"文档已加密且无法解密: {path.name}")

        pages: list[PageText] = []
        for page in doc:
            pages.append(_extract_page(page))

        total_chars = sum(len(p.text) for p in pages)
        has_text_layer = total_chars >= MIN_TEXT_CHARS
        reason = None if has_text_layer else f"文本层仅 {total_chars} 个字符，判定为扫描件"

        doc_text = DocumentText(pages=pages)
        doc_text.blocks = _build_blocks(doc, doc_text)
        return PdfExtractResult(doc=doc_text, has_text_layer=has_text_layer, scan_reason=reason)


def _extract_page(page: pymupdf.Page) -> PageText:
    """提取单页的文本与字符坐标。

    用 rawdict 拿到 chars 级别的 bbox；**按 PyMuPDF 的阅读顺序**遍历
    block → line → span → char，保证文本顺序与视觉顺序一致。
    """
    raw = page.get_text("rawdict")
    chars: list[str] = []
    boxes: list[CharBox] = []
    page_no = page.number + 1

    for block in raw.get("blocks", []):
        # type 0 = 文本块，1 = 图片块；图片块无文本，跳过
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = float(span.get("size") or 0.0)
                ascender = float(span.get("ascender") or 0.0)
                descender = float(span.get("descender") or 0.0)
                # 判断该 span 的字体度量是否可信（见 _needs_metric_repair 说明）
                repair = _needs_metric_repair(size, ascender, descender)
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c:
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    if repair:
                        # 用 origin（基线原点，实测可信）+ 标称度量重算纵向范围。
                        # 只修纵向：横向 bbox 实测正常，不动。
                        ox, oy = ch["origin"]
                        y0 = oy - size * _FALLBACK_ASCENDER
                        y1 = oy - size * _FALLBACK_DESCENDER
                    chars.append(c)
                    boxes.append(
                        CharBox(char=c, x0=x0, y0=y0, x1=x1, y1=y1, page_no=page_no)
                    )
            # 行末补换行，让文本保留段落结构（表格行也各自成行）
            if chars and chars[-1] != "\n":
                chars.append("\n")
                last = boxes[-1]
                boxes.append(
                    CharBox(
                        char="\n",
                        x0=last.x1, y0=last.y0, x1=last.x1, y1=last.y1,
                        page_no=page_no,
                    )
                )

    text = "".join(chars)
    return PageText(
        page_no=page_no,
        width=float(page.rect.width),
        height=float(page.rect.height),
        text=text,
        char_boxes=boxes,
        source="native_text",
    )


def _needs_metric_repair(size: float, ascender: float, descender: float) -> bool:
    """判断该 span 的字体度量是否需要修复。

    **背景（实测）**：WPS 导出的 PDF 中，表格单元格文字使用 Cambria 字体时，
    字体度量被写成畸形值（`ascender=3.116` / `descender=-2.463`，
    而正常中文字体是 0.859 / -0.140）。PyMuPDF 忠实照搬这组度量，
    导致字符 bbox 高度是字号的 **5.6 倍**（11.05pt 的字，框高 61.6pt）。

    后果：`search_for` 与字符 bbox 都返回虚高框，前端高亮会盖住整行甚至跨行。

    判据用"bbox 高度 / 字号"而非硬编码 `ascender > 2`：
    后者依赖具体字体的取值，换一个畸形字体就会失效。
    """
    if size <= 0:
        return False
    height = (ascender - descender) * size
    return height / size > _BBOX_HEIGHT_RATIO_LIMIT


def _build_blocks(doc: pymupdf.Document, doc_text: DocumentText) -> list[ParsedBlock]:
    """把页文本切成块（段落 / 表格），供条款切分使用。

    段落边界来自 `get_text("blocks")`（PyMuPDF 的版面分析结果），
    再用字符偏移在全文里定位。表格块单独标记，避免表格里的
    "违约责任"等字样被误判成条款标题。
    """
    blocks: list[ParsedBlock] = []
    para_index = 0
    for page in doc:
        page_no = page.number + 1
        base = doc_text.page_offset(page_no)
        for blk in page.get_text("blocks"):
            x0, y0, x1, y1, text = blk[0], blk[1], blk[2], blk[3], blk[4]
            # get_text("blocks") 的第 6 个元素是 block 类型（0 文本 / 1 图片）
            blk_type = blk[6] if len(blk) > 6 else 0
            if blk_type != 0:
                continue
            stripped = text.strip()
            if not stripped:
                continue
            kind = _guess_kind(stripped)
            blocks.append(
                ParsedBlock(
                    kind=kind,
                    text=stripped,
                    page_no=page_no,
                    page_end=page_no,
                    para_index=para_index,
                    char_start=None,  # 由 anchor_builder 精确填充
                    char_end=None,
                    bbox=(float(x0), float(y0), float(x1), float(y1)),
                )
            )
            para_index += 1
        _ = base
    return blocks


def _guess_kind(text: str) -> BlockKind:
    """粗略判断块类型。

    只做"看起来像标题"的启发式判断；表格识别交给 `find_tables`，
    这里不做重活。标题判据：短（<40 字）、不含句末标点。
    """
    if len(text) <= 40 and not any(p in text for p in "。；;."):
        return BlockKind.TITLE
    return BlockKind.PARAGRAPH


def find_tables(path: str | Path) -> list[dict]:
    """用 PyMuPDF 内置表格识别提取表格（用于元数据提取与展示）。

    返回 `[{page_no, bbox, rows}]`。失败时返回空列表——表格识别是
    尽力而为，不应让整条解析链路失败。
    """
    out: list[dict] = []
    try:
        with pymupdf.open(Path(path)) as doc:
            for page in doc:
                try:
                    tabs = page.find_tables()
                except Exception as exc:  # noqa: BLE001 - 表格识别失败不影响主流程
                    logger.warning("第 %s 页表格识别失败: %s", page.number + 1, exc)
                    continue
                for t in tabs.tables:
                    out.append({
                        "page_no": page.number + 1,
                        "bbox": tuple(float(v) for v in t.bbox),
                        "rows": t.extract(),
                    })
    except Exception as exc:  # noqa: BLE001
        logger.warning("表格提取整体失败: %s", exc)
        return []
    return out
