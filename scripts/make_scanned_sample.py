"""生成"扫描件"示例：把 DOCX 转换后的 PDF 逐页栅格化，得到无文本层的 PDF。

**为什么要一个生成脚本**：`samples/scanned/` 里是二进制产物，直接提交二进制
无法审阅其来源。本脚本记录它是怎么来的（同一份 DOCX 经 WPS 排版后栅格化），
将来示例合同更新时可一键重生成。

用法：
    backend\\.venv\\Scripts\\python.exe scripts\\make_scanned_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

# Windows 控制台默认 GBK，直接 print ✓ 会抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.services.parsing.docx_converter import convert_docx  # noqa: E402

#: 栅格化 DPI。与 `.env` 的 OCR_DPI 同值，让"生成的扫描件"与"OCR 的渲染 DPI"
#: 一致——DPI 相差过大时 OCR 精度会下降，示例文件应当代表正常输入。
RASTER_DPI = 200

SOURCE_DOCX = PROJECT_ROOT / "samples" / "purchase" / "设备采购合同-高风险样本.docx"
TARGET_PDF = PROJECT_ROOT / "samples" / "scanned" / "设备采购合同-扫描件.pdf"


def main() -> int:
    if not SOURCE_DOCX.exists():
        print(f"✗ 源文件不存在: {SOURCE_DOCX}")
        return 1

    conv = convert_docx(SOURCE_DOCX, preferred=settings.docx_converter)
    if not conv.succeeded or not conv.pdf_bytes:
        print(f"✗ DOCX 转换失败: {conv.errors}")
        return 1

    src = pymupdf.open(stream=conv.pdf_bytes, filetype="pdf")
    out = pymupdf.open()
    zoom = RASTER_DPI / 72.0
    for page in src:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        img = pymupdf.open(stream=pix.tobytes("png"), filetype="png")
        wrapped = img.convert_to_pdf()  # 图片包成单页 PDF
        img.close()
        out.insert_pdf(pymupdf.open(stream=wrapped, filetype="pdf"))
    src.close()

    TARGET_PDF.parent.mkdir(parents=True, exist_ok=True)
    out.save(TARGET_PDF)
    out.close()

    # 自检：产物必须**没有**文本层，否则"扫描件"是假的，测不出 OCR 链路
    with pymupdf.open(TARGET_PDF) as chk:
        chars = sum(len(p.get_text()) for p in chk)
        print(f"✓ 已生成 {TARGET_PDF.relative_to(PROJECT_ROOT)}"
              f"（{TARGET_PDF.stat().st_size} 字节，{chk.page_count} 页，"
              f"文本层 {chars} 字符）")
        if chars:
            print("✗ 产物含文本层，不是扫描件")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
