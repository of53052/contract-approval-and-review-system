"""OCR 链路测试：块构建、坐标换算、缓存、来源标注、端到端。

设计依据：docs/architecture.md §5.2（OCR 实测 6.08s/页）、§6.3（坐标映射）、
§18.2.4（批次 8）。

**为什么这个文件重要**：OCR 引擎本身早已实现，但**接入面**有过 6 处断裂
（最严重的是 OCR 不产出 `blocks` → 扫描件切出 0 条条款，任务却报 completed）。
这些用例正是钉住这些接缝，防止回归。

**不依赖真实 OCR 的用例**优先：RapidOCR 单页约 6s，全链路用例按需 skip，
其余用合成的 `OcrPageResult` 验证纯逻辑（坐标换算、块构建、来源标注）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import Clause, Contract, ReviewTask
from app.models.enums import BusinessType, ContractSource, FileFormat, TaskStatus
from app.services.parsing.anchor_builder import AnchorBuilder
from app.services.parsing.ocr_engine import (
    OcrEngine,
    OcrPageResult,
    build_ocr_blocks,
)
from app.services.parsing.ocr_engine import (
    RETURN_WORD_BOX,
    _split_line_box,
)
from app.core.redis_client import OCR_CACHE_VERSION, key_ocr_cache
from app.services.parsing.types import CharBox, DocumentText, PageText
from app.services.review.clause_splitter import extract_metadata, split_clauses

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANNED_PDF = PROJECT_ROOT / "samples" / "scanned" / "设备采购合同-扫描件.pdf"


# ==================== 夹具 ====================

@pytest.fixture()
def db() -> Session:
    """提供会话，结束后清理业务数据。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in (
            "anchor", "risk_evidence", "risk_item", "clause", "contract_metadata",
            "parse_result", "task_event", "annotation", "writeback_log",
            "export_record", "llm_call_log",
        ):
            session.execute(text(f"DELETE FROM `{t}`"))
        session.execute(text("DELETE FROM review_task"))
        session.execute(text("DELETE FROM contract"))
        session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        session.commit()
        session.close()


def _ocr_doc() -> DocumentText:
    """合成一份"OCR 文档"：两行文本，字符框按等宽伪造。

    行框刻意留出纵向间隔，便于断言块 bbox 取的是**该行**的并集而非整页。
    """
    lines = ["第一条 标的物", "乙方向甲方提供设备。"]
    chars: list[CharBox] = []
    text_parts: list[str] = []
    for li, line in enumerate(lines):
        y = 100.0 + li * 30.0
        for i, ch in enumerate(line):
            chars.append(CharBox(char=ch, x0=50.0 + i * 10.0, y0=y,
                                 x1=50.0 + i * 10.0 + 9.0, y1=y + 12.0, page_no=1))
        chars.append(CharBox(char="\n", x0=50.0 + len(line) * 10.0, y0=y,
                             x1=50.0 + len(line) * 10.0, y1=y + 12.0, page_no=1))
        text_parts.append(line)
    text = "\n".join(text_parts) + "\n"
    page = PageText(page_no=1, width=595.0, height=842.0, text=text,
                    char_boxes=chars, source="ocr", confidence=0.93)
    return DocumentText(pages=[page])


# ==================== 块构建（核心接缝） ====================

def test_build_ocr_blocks_produces_one_block_per_line() -> None:
    """每个非空行产出一个块——不补块则下游切出 0 条条款。"""
    doc = _ocr_doc()
    assert doc.blocks == [], "初始状态应为空（dispatcher 之后才补）"

    blocks = build_ocr_blocks(doc)

    assert [b.text for b in blocks] == ["第一条 标的物", "乙方向甲方提供设备。"]
    assert [b.para_index for b in blocks] == [0, 1]
    assert all(b.page_no == 1 and b.page_end == 1 for b in blocks)


def test_build_ocr_blocks_char_span_matches_full_text() -> None:
    """块的字符区间必须能在全文里**原样取出**自己。

    区间错位会让锚点框住别的文字——这是坐标契约的核心，必须钉住。
    """
    doc = _ocr_doc()
    for blk in build_ocr_blocks(doc):
        assert doc.full_text[blk.char_start:blk.char_end] == blk.text


def test_build_ocr_blocks_bbox_covers_only_own_line() -> None:
    """块 bbox 是该行字符的并集，不能串到别的行。"""
    doc = _ocr_doc()
    first, second = build_ocr_blocks(doc)

    assert first.bbox == (50.0, 100.0, 50.0 + 7 * 10.0 - 1.0, 112.0)
    # 两行 y 区间不重叠，证明没有取整页并集
    assert first.bbox[3] <= second.bbox[1]


def test_build_ocr_blocks_skips_blank_lines() -> None:
    """空行不产出块（OCR 常吐连续换行）。"""
    page = PageText(
        page_no=1, width=595.0, height=842.0, text="\n\n  \n",
        char_boxes=[CharBox(char=c, x0=0.0, y0=0.0, x1=1.0, y1=1.0, page_no=1)
                    for c in "\n\n  \n"],
        source="ocr", confidence=0.9,
    )
    assert build_ocr_blocks(DocumentText(pages=[page])) == []


def test_ocr_blocks_make_clauses_splittable() -> None:
    """补块后能切出条款——这是"接入"与"没接入"的分水岭。"""
    doc = _ocr_doc()
    assert split_clauses(doc) == [], "无块时切不出条款"

    doc.blocks = build_ocr_blocks(doc)
    clauses = split_clauses(doc)
    # 第二行不含条款编号，归入第一条而非另起一条——切分规则与原生路径一致
    assert len(clauses) == 1
    assert clauses[0].clause_no == "第一条"
    assert "乙方向甲方提供设备。" in clauses[0].content


# ==================== 来源标注 ====================

def test_clause_source_follows_document_source() -> None:
    """OCR 文档切出的条款 source 必须是 ocr，不能写死 native_text。"""
    doc = _ocr_doc()
    doc.blocks = build_ocr_blocks(doc)
    assert {c.source for c in split_clauses(doc)} == {"ocr"}


def test_anchor_source_and_confidence_follow_document() -> None:
    """OCR 文档的锚点要带 source=ocr 与置信度，前端才会提示坐标偏差。"""
    doc = _ocr_doc()
    doc.blocks = build_ocr_blocks(doc)  # locate_block 走块表
    builder = AnchorBuilder(doc)

    hit = builder.locate("乙方向甲方提供设备。")
    assert hit.anchored
    assert hit.source.value == "ocr"
    assert hit.confidence == 0.93

    block = builder.locate_block(1, 1)
    assert block.anchored
    assert block.source.value == "ocr"
    assert block.confidence == 0.93


def test_native_document_anchors_stay_native() -> None:
    """原生文本层文档不受影响：source 仍是 native_text、置信度为 None。"""
    page = PageText(
        page_no=1, width=595.0, height=842.0, text="第一条 标的物",
        char_boxes=[CharBox(char=c, x0=i * 10.0, y0=0.0, x1=i * 10.0 + 9.0,
                            y1=12.0, page_no=1) for i, c in enumerate("第一条 标的物")],
        source="native_text",
    )
    builder = AnchorBuilder(DocumentText(pages=[page]))
    hit = builder.locate("第一条 标的物")
    assert hit.source.value == "native_text"
    assert hit.confidence is None


def test_ocr_metadata_gets_confidence_and_review_flag() -> None:
    """OCR 元数据带页置信度；低于阈值时置 need_review。"""
    page = PageText(
        page_no=1, width=595.0, height=842.0,
        text="甲方：某某科技有限公司\n合同编号：CG-2026-0001\n",
        char_boxes=[CharBox(char=c, x0=i * 10.0, y0=0.0, x1=i * 10.0 + 9.0,
                            y1=12.0, page_no=1)
                    for i, c in enumerate("甲方：某某科技有限公司\n合同编号：CG-2026-0001\n")],
        source="ocr", confidence=0.62,
    )
    items = extract_metadata(DocumentText(pages=[page]))
    assert items, "应提取到元数据"
    assert all(m.confidence == pytest.approx(0.62) for m in items)
    assert all(m.need_review for m in items), "置信度 0.62 低于门槛，应标记待核对"


def test_native_metadata_never_needs_review() -> None:
    """原生文本层的元数据置信度恒为 1.0，不标待核对。"""
    body = "甲方：某某科技有限公司\n合同编号：CG-2026-0001\n"
    page = PageText(
        page_no=1, width=595.0, height=842.0, text=body,
        char_boxes=[CharBox(char=c, x0=i * 10.0, y0=0.0, x1=i * 10.0 + 9.0,
                            y1=12.0, page_no=1) for i, c in enumerate(body)],
        source="native_text",
    )
    items = extract_metadata(DocumentText(pages=[page]))
    assert items
    assert all(m.confidence == 1.0 and not m.need_review for m in items)


# ==================== 坐标换算 ====================

def test_ocr_coordinate_scale_converts_pixels_to_points() -> None:
    """OCR 像素坐标 × (72/DPI) = PDF point。"""
    eng = OcrEngine(dpi=200)
    assert eng._scale == pytest.approx(72.0 / 200.0)

    eng72 = OcrEngine(dpi=72)
    assert eng72._scale == pytest.approx(1.0), "72 DPI 时像素即 point"


def test_ocr_engine_rejects_non_positive_dpi() -> None:
    """DPI 必须为正，否则缩放系数无意义。"""
    for bad in (0, -1):
        with pytest.raises(ValueError):
            OcrEngine(dpi=bad)


def test_ocr_page_result_json_roundtrip_preserves_char_boxes() -> None:
    """缓存序列化必须保住字符框——只存文本会让二次命中丢失高亮坐标。"""
    original = OcrPageResult(
        page_no=2, width=595.0, height=842.0, text="甲乙\n", avg_confidence=0.88,
        char_boxes=[
            CharBox(char="甲", x0=1.0, y0=2.0, x1=3.0, y1=4.0, page_no=2),
            CharBox(char="乙", x0=5.0, y0=6.0, x1=7.0, y1=8.0, page_no=2),
        ],
    )
    restored = OcrPageResult.from_json(original.to_json())
    assert restored == original


# ==================== 字符级框（坐标精度，R14'） ====================
#
# 背景：早期实现按"行内等宽"把行框切给各字符，抹平了汉字（1em）与
# 数字/字母（0.2~0.6em）的宽度差。混排行会累积漂移——实测含金额/编号的
# 字段框左边界前移 37~63pt，用户可见为"提取字段的框往前偏移"。
# 现在改用 OCR 自身的字/词框（return_word_box），本组用例钉住这条接缝。

def test_return_word_box_is_enabled() -> None:
    """字符级框必须开启——关掉则坐标退化为等宽估算（回归到 R14 的缺陷）。"""
    assert RETURN_WORD_BOX is True


def test_split_line_box_uses_word_boxes_when_counts_match() -> None:
    """词框数与字符数一致时，逐字采用真实词框，而不是等宽切分。

    构造一个"行框内宽度分布不均"的行：前 4 个汉字各占 20px，
    末尾数字只占 5px。等宽模型会把数字框推到很右边；字符级模型不会。
    """
    text = "金额2"
    # 行框 0~85px：汉字 20px 一个、数字 5px —— 总量刻意不等于 n 等分
    word_boxes = [
        [[0, 0], [20, 0], [20, 10], [0, 10]],
        [[20, 0], [40, 0], [40, 10], [20, 10]],
        [[75, 0], [80, 0], [80, 10], [75, 10]],
    ]
    boxes = _split_line_box(text, 0.0, 0.0, 85.0, 10.0, word_boxes, scale=1.0)
    assert len(boxes) == len(text)
    # 数字 "2" 的真实位置在 75~80，等宽切分会落在 56.7~85 这种区间
    assert boxes[2][0] == pytest.approx(75.0)
    assert boxes[2][2] == pytest.approx(80.0)


def test_split_line_box_falls_back_to_equal_width_on_count_mismatch() -> None:
    """词框数与字符数不符时必须回退等宽，而不是错位对齐。

    少数行 RapidOCR 会为空格额外产出词框（实测「第二条 合同金额」是 8 框 7 字）。
    此时逐个 zip 会把每字都对到下一位，整行文字错位——必须拒绝。
    """
    text = "第二条 合同金额"        # 7 个非空字符 + 1 个空格 = 8 字符
    word_boxes = [
        [[i * 10, 0], [i * 10 + 9, 0], [i * 10 + 9, 10], [i * 10, 10]]
        for i in range(3)          # 刻意只给 3 个，与 8 个字符不符
    ]
    boxes = _split_line_box(text, 0.0, 0.0, 80.0, 10.0, word_boxes, scale=1.0)
    assert len(boxes) == len(text)
    step = 80.0 / len(text)
    assert boxes[0] == pytest.approx((0.0, 0.0, step, 10.0))
    assert boxes[-1][2] == pytest.approx(80.0)


def test_split_line_box_without_word_boxes_is_equal_width() -> None:
    """OCR 未给词框（None/空）时回退等宽——向后兼容旧行为。"""
    for empty in (None, []):
        boxes = _split_line_box("甲乙", 0.0, 0.0, 20.0, 10.0, empty, scale=1.0)
        assert len(boxes) == 2
        assert boxes[0] == pytest.approx((0.0, 0.0, 10.0, 10.0))
        assert boxes[1] == pytest.approx((10.0, 0.0, 20.0, 10.0))


def test_split_line_box_applies_scale() -> None:
    """词框是像素坐标，必须乘 (72/DPI) 换算成 PDF point。"""
    text = "甲"
    word_boxes = [[[100, 200], [150, 200], [150, 240], [100, 240]]]
    boxes = _split_line_box(text, 0.0, 0.0, 999.0, 999.0, word_boxes, scale=72.0 / 200.0)
    assert boxes[0] == pytest.approx((36.0, 72.0, 54.0, 86.4))


def test_split_line_box_empty_text_returns_empty() -> None:
    """空文本返回空列表——避免下游 `text` 与 `char_boxes` 长度不一致。"""
    assert _split_line_box("", 0.0, 0.0, 10.0, 10.0, None, scale=1.0) == []


def test_ocr_cache_key_is_versioned() -> None:
    """缓存键必须带算法版本：换坐标算法后旧条目不得再命中。

    否则修完坐标仍读到缓存里的旧（等宽）坐标，表现为"改了没效果"。
    """
    key = key_ocr_cache("abc123", 2)
    assert f"v{OCR_CACHE_VERSION}" in key
    # 与旧版（无版本段）不同，旧缓存自然失配
    assert key != "ocr:abc123:2"
    assert key.endswith(":abc123:2")


# ==================== 端到端（需真实 OCR） ====================

@pytest.mark.skipif(not SCANNED_PDF.exists(), reason="扫描件示例不存在")
def test_scanned_pdf_end_to_end(db: Session) -> None:
    """扫描件全链路：OCR → 补块 → 切分 → 规则 → LLM → 锚点 → completed。

    **这是批次 8 的验收用例**：DOCX 与扫描件跑同一份合同应得到同样的风险结论。
    """
    from app.services.llm import MockProvider
    from app.workers.pipeline import Pipeline

    raw = SCANNED_PDF.read_bytes()
    h = hashlib.sha256(b"test:ocr-e2e").hexdigest()
    c = Contract(
        title="扫描件端到端", business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.PDF.value, file_object_key="test/scanned.pdf",
        file_name=SCANNED_PDF.name, file_size=len(raw), file_hash=h,
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()

    result = Pipeline(db, t, provider=MockProvider()).run(SCANNED_PDF)

    assert result.status == TaskStatus.COMPLETED.value, result.blocked_reason
    assert result.overall_risk == "high"

    clauses = list(db.execute(
        select(Clause).where(Clause.contract_id == c.id).order_by(Clause.seq)
    ).scalars())
    assert len(clauses) >= 8, f"扫描件应切出完整条款，实际 {len(clauses)}"
    assert {cl.source for cl in clauses} == {"ocr"}
    assert all(cl.char_start is not None for cl in clauses), "OCR 块应带字符区间"
    # 条款编号识别正确（OCR 输出"第三条付款方式"这类无空格写法）
    assert "第一条" in {cl.clause_no for cl in clauses}


@pytest.mark.skipif(not SCANNED_PDF.exists(), reason="扫描件示例不存在")
def test_scanned_pdf_ocr_disabled_blocks(db: Session) -> None:
    """关掉 OCR 后，扫描件必须 blocked 而不是"成功但零风险"。"""
    from app.services.llm import MockProvider
    from app.workers.pipeline import Pipeline

    raw = SCANNED_PDF.read_bytes()
    h = hashlib.sha256(b"test:ocr-off").hexdigest()
    c = Contract(
        title="扫描件关闭OCR", business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.PDF.value, file_object_key="test/scanned.pdf",
        file_name=SCANNED_PDF.name, file_size=len(raw), file_hash=h,
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()

    result = Pipeline(db, t, provider=MockProvider(), allow_ocr=False).run(SCANNED_PDF)

    assert result.status == TaskStatus.BLOCKED.value
    assert result.blocked_reason == "empty_content"
