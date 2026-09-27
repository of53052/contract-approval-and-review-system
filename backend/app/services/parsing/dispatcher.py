"""解析入口：格式判定与路径分派。

设计依据：docs/architecture.md §5.1（四条输入路径）、§7.3（超时策略）。

**单一入口**：调用方（任务执行器）不需要关心文件格式，只调 `parse_document`。

四条路径：
    DOCX                → WPS COM 导出 PDF → PyMuPDF 提取
    文本型 PDF          → PyMuPDF 直接提取
    扫描件 PDF / 图片   → PyMuPDF 渲染 + RapidOCR（批次 8 起启用，见 `OCR_ENABLED`）

超时策略（§7.3）：
    timeout = base_timeout + per_page_timeout × page_count
    本地 CPU OCR 为分钟级，阈值不能写死。

⚠️ `compute_timeout()` 目前**无生产调用点**（仅测试引用）。COM 调用无法安全中断，
   超时只能事后标记，接线方案待决策（架构 R11）。
"""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.core.config import settings
from app.models.enums import BlockedReason, FileFormat, ParseMethod, ParseStatus
from app.services.parsing.anchor_builder import AnchorBuilder
from app.services.parsing.docx_converter import convert_docx
from app.services.parsing.ocr_engine import OcrEngine, build_ocr_blocks, is_blank_page
from app.services.parsing.pdf_extractor import EncryptedPdfError, extract_pdf
from app.services.parsing.types import DocumentText

logger = logging.getLogger(__name__)

#: 图片扩展名 → 直接走 OCR 路径
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})


class ParseBlocked(Exception):
    """解析受阻。携带 `BlockedReason` 与可读详情，由任务执行器落库。"""

    def __init__(self, reason: BlockedReason, detail: str) -> None:
        super().__init__(f"[{reason.value}] {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class ParseOutcome:
    """解析结果，字段与 `parse_result` 表一一对应。"""

    doc: DocumentText
    parse_method: ParseMethod
    page_count: int
    has_text_layer: bool
    duration_ms: int
    status: ParseStatus = ParseStatus.SUCCESS
    converter_used: str | None = None
    converter_fallback_chain: str | None = None
    ocr_engine: str | None = None
    ocr_dpi: int | None = None
    avg_confidence: float | None = None
    failed_pages: str | None = None
    error_detail: str | None = None
    #: 解析过程中的降级/异常说明，供日志与排查
    notes: list[str] = field(default_factory=list)
    #: 前端渲染用的 PDF 字节，供流水线上传 MinIO 登记 `pdf_object_key`。
    #: DOCX 路径放 WPS COM 转换产物；图片路径放"包成的单页 PDF"（PDF.js 渲染不了图片）。
    #: 原生 PDF / 扫描件不需要转换，前端直接用原件渲染，此处保持 None。
    pdf_bytes: bytes | None = None

    def build_anchor_builder(self) -> AnchorBuilder:
        """构造锚点对齐器。"""
        return AnchorBuilder(self.doc)


# ==================== 格式判定 ====================

def detect_format(path: str | Path) -> FileFormat:
    """判定文件格式。

    `pdf` 与 `scanned_pdf` 的区分需要实际打开文档看文本层，
    因此这里只按扩展名给"初步格式"，扫描件判定在解析时完成。
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return FileFormat.DOCX
    if suffix == ".pdf":
        return FileFormat.PDF
    if suffix in IMAGE_SUFFIXES:
        return FileFormat.IMAGE
    raise ValueError(f"不支持的文件格式: {suffix}")


def compute_timeout(page_count: int) -> int:
    """按页数计算动态超时（秒）。见 §7.3。"""
    return settings.parse_base_timeout + settings.parse_per_page_timeout * max(page_count, 1)


# ==================== 主入口 ====================

def parse_document(
    path: str | Path,
    *,
    original_name: str | None = None,
    allow_ocr: bool | None = None,
    file_hash: str | None = None,
) -> ParseOutcome:
    """解析文档，返回统一的段落树与锚点数据源。

    `allow_ocr` 为 None 时取 `.env` 的 `OCR_ENABLED`（默认 true，PRD 2.4.10
    要求支持扫描件）。置 false 时扫描件与图片直接抛 `ParseBlocked`，
    便于在无 OCR 依赖的环境降级。

    `file_hash` 用于 OCR 结果缓存（同一文件重复解析直接命中，省 6s/页）。
    """
    path = Path(path)
    if allow_ocr is None:
        allow_ocr = settings.ocr_enabled
    fmt = detect_format(path)
    logger.info("开始解析: %s（格式 %s，OCR %s）",
                original_name or path.name, fmt.value, "启用" if allow_ocr else "关闭")

    if fmt == FileFormat.DOCX:
        return _parse_docx(path, allow_ocr=allow_ocr)
    if fmt == FileFormat.PDF:
        return _parse_pdf(path, allow_ocr=allow_ocr, file_hash=file_hash)
    if fmt == FileFormat.IMAGE:
        return _parse_image(path, allow_ocr=allow_ocr, file_hash=file_hash)
    raise ValueError(f"未处理格式: {fmt}")


# ==================== 各路径实现 ====================

def _parse_pdf(
    path: Path, *, allow_ocr: bool, file_hash: str | None = None
) -> ParseOutcome:
    """文本型 PDF 路径。"""
    t0 = time.perf_counter()

    try:
        result = extract_pdf(path)
    except EncryptedPdfError as exc:
        raise ParseBlocked(BlockedReason.ENCRYPTED, str(exc)) from exc

    if not result.has_text_layer:
        # 无文本层 -> 扫描件
        if not allow_ocr:
            raise ParseBlocked(
                BlockedReason.EMPTY_CONTENT,
                f"{result.scan_reason}；OCR 链路已关闭（OCR_ENABLED=false）",
            )
        return _ocr_pdf(path, t0, file_hash=file_hash)

    duration_ms = int((time.perf_counter() - t0) * 1000)
    _assert_not_blank(result.doc)
    return ParseOutcome(
        doc=result.doc,
        parse_method=ParseMethod.PDF_NATIVE,
        page_count=result.doc.page_count,
        has_text_layer=True,
        duration_ms=duration_ms,
    )


def _parse_docx(path: Path, *, allow_ocr: bool) -> ParseOutcome:
    """DOCX 路径：先转 PDF，再按文本 PDF 提取。

    ⚠️ DOCX 本身不含分页信息，必须经排版引擎渲染才有真实页码。
    """
    t0 = time.perf_counter()
    conv = convert_docx(path, preferred=settings.docx_converter)

    if not conv.succeeded:
        # 全部转换器失败：唯一可能是 passthrough 生效（无分页）
        if "passthrough" in conv.fallback_chain:
            raise ParseBlocked(
                BlockedReason.CONVERTER_UNAVAILABLE,
                "WPS COM 与 LibreOffice 均不可用，无法确定分页；"
                f"详情: {'; '.join(conv.errors)}",
            )
        raise ParseBlocked(
            BlockedReason.ERROR,
            f"DOCX 转换失败: {'; '.join(conv.errors)}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "converted.pdf"
        pdf_path.write_bytes(conv.pdf_bytes or b"")
        try:
            extracted = extract_pdf(pdf_path)
        except EncryptedPdfError as exc:  # 转换产物不应加密
            raise ParseBlocked(BlockedReason.ENCRYPTED, str(exc)) from exc

    duration_ms = int((time.perf_counter() - t0) * 1000)
    _assert_not_blank(extracted.doc)

    method = (
        ParseMethod.DOCX_WPS_COM
        if conv.converter_used == "wps_com"
        else ParseMethod.DOCX_LIBREOFFICE
        if conv.converter_used == "libreoffice"
        else ParseMethod.DOCX_PASSTHROUGH
    )
    notes = []
    if len(conv.fallback_chain) > 1:
        notes.append(
            f"转换器发生降级（{conv.chain_str}）；注意不同引擎的分页可能不同，"
            "报告中的页码以实际生效的转换器为准"
        )
    return ParseOutcome(
        doc=extracted.doc,
        parse_method=method,
        page_count=extracted.doc.page_count,
        has_text_layer=extracted.has_text_layer,
        duration_ms=duration_ms,
        converter_used=conv.converter_used,
        converter_fallback_chain=conv.chain_str,
        notes=notes,
        pdf_bytes=conv.pdf_bytes,
    )


def _parse_image(
    path: Path, *, allow_ocr: bool, file_hash: str | None = None
) -> ParseOutcome:
    """图片路径：先包成单页 PDF，再走 OCR。"""
    if not allow_ocr:
        raise ParseBlocked(
            BlockedReason.EMPTY_CONTENT,
            "图片输入需要 OCR；OCR 链路已关闭（OCR_ENABLED=false）",
        )
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "image.pdf"
        # 用 PyMuPDF 把图片包成 PDF，统一后续处理
        img_doc = pymupdf.open(path)
        pdf_bytes = img_doc.convert_to_pdf()
        img_doc.close()
        pdf_path.write_bytes(pdf_bytes)
        outcome = _ocr_pdf(pdf_path, t0, source_path=path, file_hash=file_hash)
        # 图片原件前端渲染不了（PDF.js 只吃 PDF），把包好的 PDF 一并带回，
        # 由流水线上传 MinIO 登记 `pdf_object_key`——否则工作台显示"正文无法渲染"。
        # 扫描件 PDF 不需要：原件本身就是 PDF。
        outcome.pdf_bytes = pdf_bytes
        return outcome


def _ocr_pdf(
    path: Path,
    t0: float,
    *,
    source_path: Path | None = None,
    file_hash: str | None = None,
) -> ParseOutcome:
    """OCR 路径（扫描件 / 图片）。批次 8 起由 `OCR_ENABLED` 控制启用。"""
    dpi = settings.ocr_dpi
    engine = OcrEngine(dpi=dpi)

    try:
        results = engine.recognize_pdf(path, file_hash=file_hash)
    except Exception as exc:  # noqa: BLE001 - OCR 失败需转为 blocked 而非崩溃
        raise ParseBlocked(
            BlockedReason.ERROR, f"OCR 执行失败: {type(exc).__name__}: {exc}"
        ) from exc

    pages = [engine.to_page_text(r) for r in results]
    confidences = [r.avg_confidence for r in results if r.avg_confidence > 0]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

    # 严重模糊判定：平均置信度过低
    from app.services.parsing.ocr_engine import MIN_AVG_CONFIDENCE

    if avg_conf and avg_conf < MIN_AVG_CONFIDENCE:
        raise ParseBlocked(
            BlockedReason.BLURRED,
            f"OCR 平均置信度 {avg_conf:.2f} 低于阈值 {MIN_AVG_CONFIDENCE}",
        )

    doc = DocumentText(pages=pages)
    _assert_not_blank(doc)
    # OCR 只有逐行文字，没有版面分析结果；不补块则下游切不出任何条款
    doc.blocks = build_ocr_blocks(doc)

    duration_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        "OCR 解析完成: %s 页 / %s 块 / 平均置信度 %.3f",
        doc.page_count, len(doc.blocks), avg_conf,
    )
    return ParseOutcome(
        doc=doc,
        parse_method=ParseMethod.OCR_RAPIDOCR,
        page_count=doc.page_count,
        has_text_layer=False,
        duration_ms=duration_ms,
        ocr_engine=settings.ocr_engine,
        ocr_dpi=dpi,
        avg_confidence=avg_conf or None,
        notes=[
            f"OCR 识别结果，坐标为 OCR 检测框换算（字/词级，非像素级字形边界）；"
            f"来源: {source_path.name if source_path else path.name}"
        ],
    )


def _assert_not_blank(doc: DocumentText) -> None:
    """全部页面均近似空白时抛 blocked(empty_content)。"""
    if not doc.pages:
        raise ParseBlocked(BlockedReason.EMPTY_CONTENT, "文档不含任何页面")
    if all(is_blank_page(p) for p in doc.pages):
        raise ParseBlocked(BlockedReason.EMPTY_CONTENT, "全部页面均无可提取文本")
