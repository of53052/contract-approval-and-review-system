"""扫描件 OCR 引擎（RapidOCR）。

设计依据：docs/architecture.md §5.2（实测 6.08s/页）、§6.3（坐标映射）。

**坐标映射是本模块唯一职责**：OCR 返回像素坐标，必须在进入解析层之前
换算成 PDF point，否则前端渲染会错位。

    OCR 像素坐标 × (72 / DPI) = PDF point 坐标

实测误差 0.2~5.1 pt，属检测框 padding 的正常范围。

**批次 8 起启用**：dispatcher 的扫描件 / 图片路径会路由到本模块，
开关为 `.env` 的 `OCR_ENABLED`（默认 true）。

除了坐标映射，本模块还负责**把 OCR 行文本组装成块**（`build_ocr_blocks`）：
原生 PDF 的段落边界来自 PyMuPDF 版面分析，而 OCR 只有"一行行文字 + 字符框"，
不补块的话下游 `split_clauses` 会切出 0 条条款。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.services.parsing.types import (
    CharBox,
    DocumentText,
    PageText,
    ParsedBlock,
    guess_block_kind,
)

logger = logging.getLogger(__name__)

#: PDF 内部单位：1 point = 1/72 inch
POINTS_PER_INCH = 72.0

#: 低于此置信度的识别结果整页标记为低质量，触发 blocked(blurred)
MIN_AVG_CONFIDENCE = 0.5


@dataclass
class OcrPageResult:
    """单页 OCR 结果。"""

    page_no: int
    width: float
    height: float
    text: str
    char_boxes: list[CharBox]
    avg_confidence: float

    def to_json(self) -> str:
        """序列化为 JSON，供 Redis 缓存。

        **为什么缓存整个结果而非只缓存文本**：前端高亮依赖 `char_boxes`
        （OCR 不提供字符级坐标，这些框是等宽切分算出来的）。只缓存文本
        会让二次命中退化为"有文字、无坐标"，高亮全丢。
        """
        return json.dumps({
            "page_no": self.page_no,
            "width": self.width,
            "height": self.height,
            "text": self.text,
            "avg_confidence": self.avg_confidence,
            "char_boxes": [
                [b.char, b.x0, b.y0, b.x1, b.y1, b.page_no] for b in self.char_boxes
            ],
        }, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_json(raw: str) -> "OcrPageResult":
        d = json.loads(raw)
        return OcrPageResult(
            page_no=d["page_no"],
            width=d["width"],
            height=d["height"],
            text=d["text"],
            avg_confidence=d["avg_confidence"],
            char_boxes=[
                CharBox(char=c, x0=x0, y0=y0, x1=x1, y1=y1, page_no=pn)
                for c, x0, y0, x1, y1, pn in d["char_boxes"]
            ],
        )


class OcrEngine:
    """RapidOCR 封装。

    **延迟初始化**：RapidOCR 首次实例化约 2.4s（加载 onnx 模型），
    模块级实例化会拖慢所有导入本模块的代码（包括单元测试）。
    """

    def __init__(self, dpi: int = 200) -> None:
        if dpi <= 0:
            raise ValueError(f"DPI 必须为正数，收到 {dpi}")
        self.dpi = dpi
        self._scale = POINTS_PER_INCH / dpi
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def recognize_pdf(
        self, path: str | Path, *, file_hash: str | None = None
    ) -> list[OcrPageResult]:
        """对整份 PDF 逐页 OCR。

        每页渲染为位图后识别；坐标乘 `_scale` 换算为 PDF point。

        `file_hash` 非空时按 `ocr:{hash}:{page}` 读写 Redis 缓存：
        本地 CPU 单页约 6s，同一文件重复上传/重试不必重跑（架构 §10.2）。
        缓存读写失败只记警告，**不影响 OCR 本身**。
        """
        results: list[OcrPageResult] = []
        with pymupdf.open(Path(path)) as doc:
            for page in doc:
                cached = _cache_get(file_hash, page.number + 1)
                if cached is not None:
                    results.append(cached)
                    continue
                result = self.recognize_page(page)
                _cache_put(file_hash, result)
                results.append(result)
        return results

    def recognize_page(self, page: pymupdf.Page) -> OcrPageResult:
        """对单页 OCR。

        渲染 DPI 决定图像尺寸，`page.rect` 仍是 PDF point 尺寸——
        两者之比即 `_scale` 的来源。
        """
        # 用 matrix 缩放渲染，避免落盘中间图
        zoom = self.dpi / POINTS_PER_INCH
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        img_bytes = pix.tobytes("png")

        engine = self._get_engine()
        raw_result, _ = engine(img_bytes)

        page_no = page.number + 1
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)

        if not raw_result:
            logger.warning("第 %s 页 OCR 未识别出任何文本", page_no)
            return OcrPageResult(
                page_no=page_no, width=page_w, height=page_h,
                text="", char_boxes=[], avg_confidence=0.0,
            )

        chars: list[str] = []
        boxes: list[CharBox] = []
        confidences: list[float] = []

        for item in raw_result:
            # RapidOCR 返回 [box, text, score]；box 是 4 点多边形 [[x,y], ...]
            box, text, score = item[0], item[1], float(item[2])
            confidences.append(score)
            if not text:
                continue
            # 该行文本的外接矩形（像素），换算到 PDF point
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x0 = min(xs) * self._scale
            y0 = min(ys) * self._scale
            x1 = max(xs) * self._scale
            y1 = max(ys) * self._scale

            # OCR 不提供字符级坐标，按等宽把行框切分给各字符。
            # 这是**近似**：前端对 OCR 来源的高亮会标"识别定位，可能有偏差"。
            n = len(text)
            if n == 0:
                continue
            step = (x1 - x0) / n
            for i, c in enumerate(text):
                chars.append(c)
                boxes.append(CharBox(
                    char=c,
                    x0=x0 + step * i,
                    y0=y0,
                    x1=x0 + step * (i + 1),
                    y1=y1,
                    page_no=page_no,
                ))
            chars.append("\n")
            boxes.append(CharBox(char="\n", x0=x1, y0=y0, x1=x1, y1=y1, page_no=page_no))

        text = "".join(chars)
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        logger.info(
            "第 %s 页 OCR 完成: %s 字符, 平均置信度 %.3f", page_no, len(text), avg_conf
        )
        return OcrPageResult(
            page_no=page_no, width=page_w, height=page_h,
            text=text, char_boxes=boxes, avg_confidence=avg_conf,
        )

    def to_page_text(self, result: OcrPageResult) -> PageText:
        """把 OCR 结果转成解析层统一的 PageText。

        `source="ocr"` 与 `confidence` 一并带上：前者让下游把条款与锚点
        标成 OCR 来源（前端据此提示"识别定位，可能有偏差"），
        后者用于元数据的 `need_review` 判定。
        """
        return PageText(
            page_no=result.page_no,
            width=result.width,
            height=result.height,
            text=result.text,
            char_boxes=result.char_boxes,
            source="ocr",
            confidence=result.avg_confidence or None,
        )


def is_blank_page(page: PageText) -> bool:
    """判断页面是否近似空白（去掉空白后不足 10 个字符）。

    用于触发 `BlockedReason.empty_content`。
    """
    return len(page.text.replace("\n", "").strip()) < 10


# ==================== OCR 结果缓存（Redis） ====================


def _cache_get(file_hash: str | None, page_no: int) -> OcrPageResult | None:
    """读缓存。任何异常（Redis 不可用、反序列化失败）都视为未命中。"""
    if not file_hash:
        return None
    try:
        from app.core.redis_client import get_redis, key_ocr_cache

        raw = get_redis().get(key_ocr_cache(file_hash, page_no))
        if raw is None:
            return None
        return OcrPageResult.from_json(raw)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不应阻断 OCR
        logger.warning("OCR 缓存读取失败（忽略，走真实识别）: %s", exc)
        return None


def _cache_put(file_hash: str | None, result: OcrPageResult) -> None:
    """写缓存。失败只记警告——缓存是加速手段，不是正确性依赖。"""
    if not file_hash:
        return
    try:
        from app.core.redis_client import get_redis, key_ocr_cache, TTL_OCR_CACHE

        get_redis().set(
            key_ocr_cache(file_hash, result.page_no),
            result.to_json(),
            ex=TTL_OCR_CACHE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 缓存写入失败（忽略）: %s", exc)


# ==================== OCR 行 → 块 ====================


def build_ocr_blocks(doc: DocumentText) -> list[ParsedBlock]:
    """把 OCR 结果的每一行组装成 `ParsedBlock`，供条款切分使用。

    **为什么必须补这一步**：原生 PDF 的段落边界来自 PyMuPDF 的版面分析
    （`get_text("blocks")`），而 OCR 路径只产出"逐行文字 + 字符框"，
    `DocumentText.blocks` 默认为空。下游 `split_clauses` 只遍历 `blocks`，
    不补块的话扫描件会**切出 0 条条款**——解析"成功"但审查几乎无输入。

    粒度取"行"而非"段"：OCR 无法可靠判断段落归属（行间距、缩进都不稳），
    而条款切分本身按"条款编号起新条款"的规则工作，行粒度已经足够，
    且不会因为错误的段落合并把两条条款粘成一条。

    行框 = 该行所有非空白字符框的并集，与 `PageText.bbox_for_range` 口径一致。
    """
    blocks: list[ParsedBlock] = []
    para_index = 0
    for page in doc.pages:
        base = doc.page_offset(page.page_no)
        # 逐行扫描，同时维护行首在页内的字符下标
        line_start = 0
        for line in page.text.split("\n"):
            line_len = len(line)
            stripped = line.strip()
            if stripped:
                # 去掉行首空白后收窄区间，否则锚点会框住前导空格
                lead = line_len - len(line.lstrip())
                start = line_start + lead
                end = start + len(stripped)
                boxes = [
                    b for b in page.char_boxes[start:end] if not b.char.isspace()
                ]
                bbox = None
                if boxes:
                    bbox = (
                        min(b.x0 for b in boxes),
                        min(b.y0 for b in boxes),
                        max(b.x1 for b in boxes),
                        max(b.y1 for b in boxes),
                    )
                blocks.append(ParsedBlock(
                    kind=guess_block_kind(stripped),
                    text=stripped,
                    page_no=page.page_no,
                    page_end=page.page_no,
                    para_index=para_index,
                    # 全文坐标：与 PageText/AnchorBuilder 的坐标系一致
                    char_start=base + start,
                    char_end=base + end,
                    bbox=bbox,
                ))
                para_index += 1
            line_start += line_len + 1  # +1 = 被 split 吃掉的换行符
    return blocks
