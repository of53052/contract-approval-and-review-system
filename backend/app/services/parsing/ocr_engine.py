"""扫描件 OCR 引擎（RapidOCR）。

设计依据：docs/architecture.md §5.2（实测 6.08s/页）、§6.3（坐标映射）。

**坐标映射是本模块唯一职责**：OCR 返回像素坐标，必须在进入解析层之前
换算成 PDF point，否则前端渲染会错位。

    OCR 像素坐标 × (72 / DPI) = PDF point 坐标

实测误差 0.2~5.1 pt，属检测框 padding 的正常范围。

⚠️ **阶段一不启用**：按 docs/architecture.md §18.1，扫描件 OCR 链路
在阶段一明确不做。本模块已实现并测试，但 dispatcher 暂不路由到它。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.services.parsing.types import CharBox, PageText

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

    def recognize_pdf(self, path: str | Path) -> list[OcrPageResult]:
        """对整份 PDF 逐页 OCR。

        每页渲染为位图后识别；坐标乘 `_scale` 换算为 PDF point。
        """
        results: list[OcrPageResult] = []
        with pymupdf.open(Path(path)) as doc:
            for page in doc:
                results.append(self.recognize_page(page))
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
        """把 OCR 结果转成解析层统一的 PageText。"""
        return PageText(
            page_no=result.page_no,
            width=result.width,
            height=result.height,
            text=result.text,
            char_boxes=result.char_boxes,
            source="ocr",
        )


def is_blank_page(page: PageText) -> bool:
    """判断页面是否近似空白（去掉空白后不足 10 个字符）。

    用于触发 `BlockedReason.empty_content`。
    """
    return len(page.text.replace("\n", "").strip()) < 10
