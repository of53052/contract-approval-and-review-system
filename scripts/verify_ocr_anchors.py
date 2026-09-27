"""OCR 锚点坐标校验：按锚点 bbox 裁剪页面图像后**重新识别**，验证框内确为引用原文。

**为什么不能复用 `verify_anchors.mjs`**：那个脚本靠 PDF.js 的文本层
（`getTextContent`）取框内文字，而扫描件/图片**没有文本层**——框内恒为空，
校验必然全红。OCR 来源的锚点需要一条独立的验证路径。

**为什么改 DPI 重识别**：锚点 bbox 由 200 DPI 的 OCR 结果算出，若这里还用
200 DPI，等于同源验证，测不出"坐标换算错"。用 300 DPI 重新渲染同一区域，
坐标系一致（都是 PDF point）但像素网格不同，能真实检验 bbox 是否落在文字上。

**裁剪要外扩**：OCR 的检测框比字形略小，紧贴裁剪会把首尾字切掉，重识别时
字序错乱（实测 pad=6 时「第九条不可抗力」被读成「可抗力九条第不」，pad≥12
即正确）。这不是 bbox 错，是裁剪边界的伪影，故默认外扩 12pt。

用法：
    backend\\.venv\\Scripts\\python.exe scripts\\verify_ocr_anchors.py <合同ID> [--pdf 路径]

退出码：0 = 全部锚点覆盖到引用，1 = 存在未覆盖
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import pymupdf  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.services.parsing.ocr_engine import OcrEngine  # noqa: E402

#: 校验用 DPI。必须与解析时的 `OCR_DPI`（默认 200）**不同**，否则是同源验证。
VERIFY_DPI = 300

#: 裁剪外扩（PDF point）。见模块头说明：不外扩会因裁掉首尾字导致字序错乱。
CROP_PAD = 12.0


def _norm(s: str) -> str:
    """去全部空白，消除换行与空格的差异。"""
    return "".join(s.split())


def _match(quote: str, got: str, *, head: int = 8) -> bool:
    """判断框内文字是否与引用原文一致。

    取前 `head` 字比对：OCR 在裁剪边界处偶尔多识别或少识别一两个字，
    但那不影响"bbox 是否落在正确文字上"的结论。
    """
    want, seen = _norm(quote), _norm(got)
    if not want or not seen:
        return False
    return want[:head] in seen or seen[:head] in want


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 锚点坐标校验")
    parser.add_argument("contract_id", type=int, help="合同 ID")
    parser.add_argument("--pdf", help="渲染用 PDF 路径（默认从 MinIO 取原件）")
    args = parser.parse_args()

    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT r.title, a.seq, a.page_no, a.bbox_x0, a.bbox_y0,
                   a.bbox_x1, a.bbox_y1, a.quote_text, a.source
            FROM anchor a
            JOIN risk_item r ON a.owner_id = r.id AND a.owner_type = 'risk_item'
            WHERE r.contract_id = :cid
            ORDER BY r.seq, a.seq
        """), {"cid": args.contract_id}).all()

        if not rows:
            print(f"✗ 合同 #{args.contract_id} 没有风险锚点")
            return 1

        ocr_rows = [r for r in rows if r[8] == "ocr"]
        if not ocr_rows:
            print(f"合同 #{args.contract_id} 的锚点不是 OCR 来源（{len(rows)} 条），"
                  "请改用 run_tests.py --verify-anchors")
            return 1

        if args.pdf:
            pdf_path = Path(args.pdf)
        else:
            from app.core.config import settings
            from app.core.minio_client import get_minio
            from app.models import Contract

            c = db.get(Contract, args.contract_id)
            if c is None:
                print(f"✗ 合同 #{args.contract_id} 不存在")
                return 1
            key = c.file_object_key if c.file_format == "pdf" else c.pdf_object_key
            if not key:
                print(f"✗ 合同 #{args.contract_id} 无渲染用 PDF")
                return 1
            pdf_path = PROJECT_ROOT / "logs" / f"verify_ocr_{args.contract_id}.pdf"
            resp = get_minio().get_object(settings.minio_bucket_contracts, key)
            try:
                pdf_path.write_bytes(resp.read())
            finally:
                resp.close()
                resp.release_conn()
            print(f"（已从 MinIO 取渲染用 PDF 到 {pdf_path.name}）")

    engine = OcrEngine(dpi=VERIFY_DPI)
    passed = 0
    with pymupdf.open(pdf_path) as doc:
        for title, seq, pno, x0, y0, x1, y1, quote, source in ocr_rows:
            if source != "ocr":
                continue
            page = doc[pno - 1]
            clip = pymupdf.Rect(x0 - CROP_PAD, y0 - CROP_PAD, x1 + CROP_PAD, y1 + CROP_PAD)
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(VERIFY_DPI / 72.0, VERIFY_DPI / 72.0),
                clip=clip, alpha=False,
            )
            img = pymupdf.open(stream=pix.tobytes("png"), filetype="png")
            try:
                got = engine.recognize_page(img[0]).text
            finally:
                img.close()

            ok = _match(quote or "", got)
            passed += int(ok)
            print(f"{'✓' if ok else '✗'} [p{pno}#{seq}] {title[:20]}")
            print(f"    bbox {x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f} → 框内 {_norm(got)[:30]!r}")
            print(f"    期望 {_norm(quote or '')[:30]!r}")

    total = len(ocr_rows)
    print(f"\nOCR 锚点坐标校验: {passed}/{total} 覆盖到引用原文（{VERIFY_DPI} DPI 独立重识别）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
