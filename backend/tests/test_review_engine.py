"""审查引擎测试：状态机、重试清理、锚点降级、取高合并、防幻觉闸门、流水线端到端。

设计依据：docs/architecture.md §8（审查引擎）、§13（任务执行）。

**为什么连真实 MySQL**：状态机乐观锁、重试清理的多态 anchor 删除、
冗余计数一致性都依赖真实外键与事务行为，SQLite 跑不出问题。

**端到端用例连真实 WPS COM**：DOCX 分页只有排版引擎能给，这是本项目的
关键路径（D6）。WPS 不可用时该用例 skip，而不是假装通过。
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import (
    Anchor,
    Annotation,
    Clause,
    Contract,
    ContractMetadata,
    ParseResult,
    ReviewTask,
    RiskEvidence,
    RiskItem,
)
from app.models.enums import (
    AnchorLevel,
    BusinessType,
    ClauseType,
    ContractSource,
    FileFormat,
    MergedBy,
    RiskCategory,
    RiskLevel,
    TaskStatus,
)
from app.services.parsing.anchor_builder import AnchorBuilder
from app.services.parsing.types import BlockKind, CharBox, DocumentText, PageText, ParsedBlock
from app.services.review.clause_splitter import (
    ClauseDraft,
    MetadataDraft,
    extract_metadata,
)
from app.services.review.global_checker import HallucinationGate
from app.services.review.llm_reviewer import LlmRiskDraft
from app.services.review.merger import Merger
from app.services.review.rule_engine import RuleHit
from app.workers import state_machine as sm
from app.models.enums import MetadataKey

SAMPLE_DOCX = (
    Path(__file__).resolve().parents[2]
    / "samples" / "purchase" / "设备采购合同-高风险样本.docx"
)


# ==================== 夹具 ====================

@pytest.fixture()
def db() -> Session:
    """提供会话，结束后清理本次测试写入的全部业务数据。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in (
            "anchor", "risk_evidence", "risk_item", "annotation", "clause",
            "contract_metadata", "parse_result", "task_event", "writeback_log",
            "export_record", "llm_call_log",
        ):
            session.execute(text(f"DELETE FROM `{t}`"))
        session.execute(text("DELETE FROM review_task"))
        session.execute(text("DELETE FROM contract"))
        session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        session.commit()
        session.close()


def _make_task(db: Session, *, title: str = "测试合同") -> tuple[Contract, ReviewTask]:
    """构造合同 + 任务（pending）。"""
    h = hashlib.sha256(title.encode("utf-8")).hexdigest()
    c = Contract(
        title=title, business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.PDF.value, file_object_key=f"{title}/original.pdf",
        file_name="t.pdf", file_size=1024, file_hash=h,
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.flush()
    return c, t


def _make_doc(text: str, *, source: str = "native_text") -> DocumentText:
    """把一段文本包成单页 DocumentText，字符坐标用等宽网格伪造。

    锚点测试只关心"能否定位 + 定位到哪几个字符"，坐标数值不影响断言。
    """
    boxes = [
        CharBox(char=ch, x0=i * 10.0, y0=0.0, x1=i * 10.0 + 9.0, y1=12.0, page_no=1)
        for i, ch in enumerate(text)
    ]
    page = PageText(page_no=1, width=595.0, height=842.0, text=text,
                    char_boxes=boxes, source=source)
    blocks = [ParsedBlock(
        kind=BlockKind.PARAGRAPH, text=text, page_no=1, page_end=1,
        para_index=0, char_start=0, char_end=len(text),
        bbox=(0.0, 0.0, 500.0, 12.0),
    )]
    return DocumentText(pages=[page], blocks=blocks)


def _make_clause(content: str, *, seq: int = 0, ctype: ClauseType = ClauseType.LIABILITY) -> ClauseDraft:
    return ClauseDraft(
        clause_type=ctype, clause_no="第一条", title="违约责任", content=content,
        page_no=1, page_end=1, para_index=seq, char_start=0, char_end=len(content),
        bbox=(0.0, 0.0, 500.0, 12.0), seq=seq, source="native_text",
    )


# ==================== 状态机 ====================

def test_valid_transition_bumps_version_and_writes_event(db: Session) -> None:
    """合法流转应递增 version 并写 task_event 审计。"""
    _, t = _make_task(db)
    before = t.version
    sm.transition(db, t, TaskStatus.PARSING, operator="tester", detail="开始")
    db.flush()
    assert t.version == before + 1
    assert t.status == TaskStatus.PARSING.value
    assert t.started_at is not None, "进入 parsing 应记录 started_at"

    events = db.execute(
        select(func.count()).select_from(
            __import__("app.models", fromlist=["TaskEvent"]).TaskEvent
        ).where(
            __import__("app.models", fromlist=["TaskEvent"]).TaskEvent.task_id == t.id
        )
    ).scalar()
    assert events == 1


def test_illegal_transition_rejected(db: Session) -> None:
    """pending 直接跳 completed 必须被拒（不让状态机失控）。"""
    _, t = _make_task(db)
    with pytest.raises(sm.InvalidTransition):
        sm.transition(db, t, TaskStatus.COMPLETED)
    db.rollback()


def test_completed_is_terminal(db: Session) -> None:
    """completed 是终态，任何流转都被拒。"""
    _, t = _make_task(db)
    sm.transition(db, t, TaskStatus.PARSING)
    sm.transition(db, t, TaskStatus.REVIEWING)
    sm.transition(db, t, TaskStatus.COMPLETED)
    db.flush()
    with pytest.raises(sm.InvalidTransition):
        sm.transition(db, t, TaskStatus.PARSING)
    db.rollback()


def test_optimistic_lock_conflict(db: Session) -> None:
    """乐观锁：期望版本与实际不符时拒绝，避免并发重复执行。"""
    _, t = _make_task(db)
    stale = t.version
    sm.transition(db, t, TaskStatus.PARSING)
    db.flush()
    with pytest.raises(sm.InvalidTransition):
        sm.transition(db, t, TaskStatus.REVIEWING, expected_version=stale)
    db.rollback()


def test_block_records_reason(db: Session) -> None:
    """blocked 必须带 reason 与 detail（不变量 I1）。"""
    _, t = _make_task(db)
    sm.transition(db, t, TaskStatus.PARSING)
    sm.block(db, t, "encrypted", "文档已加密，无法解析")
    db.flush()
    assert t.status == TaskStatus.BLOCKED.value
    assert t.blocked_reason == "encrypted"
    assert "加密" in t.blocked_detail
    assert t.blocked_at is not None


def test_blocked_can_retry_to_parsing(db: Session) -> None:
    """blocked 允许人工重试回 parsing。"""
    _, t = _make_task(db)
    sm.transition(db, t, TaskStatus.PARSING)
    sm.block(db, t, "timeout", "超时")
    db.flush()
    sm.transition(db, t, TaskStatus.PARSING, operator="lawyer", detail="人工重试")
    db.flush()
    assert t.status == TaskStatus.PARSING.value


def test_mark_stale_tasks(db: Session) -> None:
    """卡住的 parsing 任务在超时后被标记为 blocked（重启恢复）。"""
    _, t = _make_task(db)
    db.execute(
        text("UPDATE review_task SET status='parsing', updated_at=NOW(3) - INTERVAL 9999 SECOND "
             "WHERE id=:i"),
        {"i": t.id},
    )
    db.commit()
    n = sm.mark_stale_tasks(db, timeout_seconds=3600)
    db.commit()
    assert n == 1
    # mark_stale_tasks 用批量 UPDATE，绕过了 ORM 身份映射；
    # 必须 expire 才能读到库里的最新值，否则拿到的是过期对象
    db.expire_all()
    refreshed = db.get(ReviewTask, t.id)
    assert refreshed.status == TaskStatus.BLOCKED.value
    assert refreshed.blocked_reason == "timeout"


# ==================== 重试清理 ====================

def test_cleanup_for_retry_no_orphan_and_preserves_history(db: Session) -> None:
    """重试清理：删旧产物（含多态 anchor），保留 parse_result 与人工批注。"""
    c, t = _make_task(db)
    pr = ParseResult(
        contract_id=c.id, task_id=t.id, parse_method="pdf_native",
        page_count=1, has_text_layer=1, status="success", duration_ms=10, attempt=1,
    )
    db.add(pr)
    db.flush()

    cl = Clause(
        contract_id=c.id, parse_result_id=pr.id, clause_type=ClauseType.LIABILITY.value,
        clause_no="第一条", title="违约责任", content="违约方赔偿全部损失。",
        page_no=1, para_index=0, seq=0, source="native_text",
    )
    db.add(cl)
    db.flush()

    meta = ContractMetadata(
        contract_id=c.id, parse_result_id=pr.id, meta_key=MetadataKey.AMOUNT.value,
        meta_value="1000", value_normalized="1000.00", value_type="decimal",
    )
    db.add(meta)
    db.flush()

    risk = RiskItem(
        contract_id=c.id, clause_id=cl.id, title="赔偿责任无上限",
        risk_level=RiskLevel.HIGH.value, category=RiskCategory.LIABILITY.value,
        reason="r", merged_by=MergedBy.RULE.value, is_global=0, unanchored=0, seq=0,
    )
    db.add(risk)
    db.flush()
    db.add(Anchor(
        owner_type="risk_item", owner_id=risk.id, seq=0, page_no=1,
        bbox_x0=1.0, bbox_y0=1.0, bbox_x1=2.0, bbox_y1=2.0,
        char_start=0, char_end=5, source="native_text", anchor_level="exact",
    ))
    db.add(Anchor(
        owner_type="contract_metadata", owner_id=meta.id, seq=0, page_no=1,
        bbox_x0=1.0, bbox_y0=1.0, bbox_x1=2.0, bbox_y1=2.0,
        char_start=0, char_end=5, source="native_text", anchor_level="exact",
    ))
    db.add(RiskEvidence(
        risk_item_id=risk.id, evidence_type="rule", rule_id=None,
        detail="规则命中", need_review=0,
    ))
    db.add(Annotation(contract_id=c.id, risk_item_id=risk.id, author="张三", content="同意"))
    db.flush()

    deleted = sm.cleanup_for_retry(db, t)
    db.flush()

    assert deleted["clause"] == 1
    assert deleted["risk_item"] == 1
    assert deleted["contract_metadata"] == 1
    assert deleted["anchor"] == 2, "多态 anchor 必须按 owner 显式清理"

    # 旧产物清空
    assert db.execute(select(func.count()).select_from(Clause)).scalar() == 0
    assert db.execute(select(func.count()).select_from(RiskItem)).scalar() == 0
    assert db.execute(select(func.count()).select_from(Anchor)).scalar() == 0
    assert db.execute(select(func.count()).select_from(ContractMetadata)).scalar() == 0

    # 历史与人工批注保留
    assert db.execute(select(func.count()).select_from(ParseResult)).scalar() == 1
    ann = db.execute(select(Annotation)).scalars().one()
    assert ann.content == "同意"
    assert ann.risk_item_id is None, "被删风险项的批注应 SET NULL 而非连带删除"

    # 任务字段重置
    assert t.blocked_reason is None and t.overall_risk is None
    assert t.high_risk_count == 0 and t.parsed_pages == 0
    assert sm.next_attempt(db, c.id) == 2


# ==================== 锚点三级降级 ====================

def test_anchor_exact_match() -> None:
    """原文逐字引用 → exact，字符区间精确。"""
    text = "乙方逾期交货的，应承担甲方全部损失。"
    doc = _make_doc(text)
    res = AnchorBuilder(doc).locate("乙方逾期交货的")
    assert res.level is AnchorLevel.EXACT
    assert res.anchored
    assert res.char_start == 0 and res.char_end == len("乙方逾期交货的")


def test_anchor_fullwidth_punctuation_normalized() -> None:
    """引用用全角标点、原文半角 → 归一化后仍能命中。"""
    doc = _make_doc("甲方应当在验收合格后支付货款。")
    res = AnchorBuilder(doc).locate("甲方应当在验收合格后支付货款。")
    assert res.anchored


def test_anchor_omitted_middle_marked_partial() -> None:
    """LLM 省略中段：锚定最长可定位片段并标 partial=True（缺陷 3 回归）。

    原文「乙方逾期交货的，应承担甲方全部损失，赔偿责任无上限。」，
    LLM 返回省略了中间 10 个字的「乙方逾期交货的，赔偿责任无上限。」——
    整段相似度仅 0.44，必须靠片段锚定才能定位。
    """
    text = "乙方逾期交货的，应承担甲方全部损失，赔偿责任无上限。"
    doc = _make_doc(text)
    omitted = "乙方逾期交货的，赔偿责任无上限。"
    res = AnchorBuilder(doc).locate(omitted)
    assert res.anchored, "省略中段的引用必须能锚定到片段"
    assert res.partial is True, "片段锚定必须标记 partial，避免误以为整句高亮"
    # 命中的片段必须是原文真实存在的连续子串
    assert res.quote_text in text


def test_anchor_unanchored_for_fabricated_quote() -> None:
    """编造内容无法定位 → level=none 且 anchored=False（绝不伪造位置）。"""
    doc = _make_doc("本合同自双方签字之日起生效。")
    res = AnchorBuilder(doc).locate("乙方应于每周五前提交财务报表并接受审计")
    assert res.level is AnchorLevel.NONE
    assert not res.anchored


def test_anchor_empty_quote_returns_none() -> None:
    """空引用直接返回 none，不报错。"""
    doc = _make_doc("任意正文。")
    assert AnchorBuilder(doc).locate("").level is AnchorLevel.NONE
    assert AnchorBuilder(doc).locate("   ").level is AnchorLevel.NONE


# ==================== 取高不取低 ====================

def _rule_hit(
    level: RiskLevel,
    clause_index: int = 0,
    *,
    title: str = "违约责任无上限",
    quote: str = "赔偿责任无上限",
    category: RiskCategory = RiskCategory.LIABILITY,
) -> RuleHit:
    """构造规则命中。

    注意：合并的配对判据是"同 clause_index 或 标题/引用相似度 ≥ 0.55"。
    要构造"不应被配对"的用例，标题与引用都得用**语义无关**的文本。
    """
    return RuleHit(
        rule_code="RULE_CODE", rule_name=title,
        category=category, risk_level=level,
        title=title, reason="规则判定", suggestion=None,
        clause_index=clause_index, quote=quote, rule_id=None,
        evidence_detail="阈值命中",
    )


def _llm_risk(
    level: RiskLevel,
    clause_index: int | None = 0,
    *,
    title: str = "赔偿责任无上限",
    quote: str = "赔偿责任无上限",
    category: RiskCategory = RiskCategory.LIABILITY,
) -> LlmRiskDraft:
    """构造 LLM 研判风险（默认与 `_rule_hit` 配对，模拟双来源）。"""
    return LlmRiskDraft(
        title=title, risk_level=level, category=category,
        reason="AI 判定", suggestion="建议设置赔偿上限", legal_basis=None,
        quote=quote, clause_index=clause_index, raw_snippet="{...}",
    )


def test_merge_takes_higher_level() -> None:
    """规则中风险 + LLM 高风险 → 高风险（取高不取低），双来源留痕。"""
    out = Merger().merge([_rule_hit(RiskLevel.MEDIUM)], [_llm_risk(RiskLevel.HIGH)])
    assert len(out.risks) == 1
    r = out.risks[0]
    assert r.risk_level is RiskLevel.HIGH
    assert r.merged_by is MergedBy.BOTH
    assert out.level_upgraded == 1
    assert {e.evidence_type for e in r.evidences} == {"rule", "llm"}


def test_merge_never_downgrades() -> None:
    """规则高风险 + LLM 低风险 → 保持高风险，不降级。"""
    out = Merger().merge([_rule_hit(RiskLevel.HIGH)], [_llm_risk(RiskLevel.LOW)])
    assert out.risks[0].risk_level is RiskLevel.HIGH
    assert out.level_upgraded == 0
    assert out.risks[0].merged_by is MergedBy.BOTH


def test_merge_keeps_llm_only_risk() -> None:
    """LLM 独有风险（规则未命中）也要保留，不能丢。

    两条风险语义无关（违约责任 vs 保密义务），不应被配对。
    """
    out = Merger().merge(
        [_rule_hit(RiskLevel.HIGH, clause_index=0)],
        [_llm_risk(
            RiskLevel.MEDIUM, clause_index=None,
            title="保密义务无期限", quote="双方均负有保密义务",
            category=RiskCategory.CONFIDENTIALITY,
        )],
    )
    assert len(out.risks) == 2
    assert out.llm_only == 1 and out.rule_only == 1
    llm_item = next(r for r in out.risks if r.merged_by is MergedBy.LLM)
    assert llm_item.risk_level is RiskLevel.MEDIUM


def test_merge_overall_risk_and_counts() -> None:
    """整体等级 = 最高项；计数与列表一致。

    三条风险语义无关（管辖 / 违约金 / 保密），各自独立成项。
    """
    out = Merger().merge(
        [
            _rule_hit(RiskLevel.LOW, 0, title="管辖地约定违规",
                      quote="提交北京仲裁委员会仲裁",
                      category=RiskCategory.JURISDICTION),
            _rule_hit(RiskLevel.HIGH, 1),
        ],
        [_llm_risk(
            RiskLevel.MEDIUM, clause_index=None,
            title="保密义务无期限", quote="双方均负有保密义务",
            category=RiskCategory.CONFIDENTIALITY,
        )],
    )
    assert out.overall_risk is RiskLevel.HIGH
    assert out.counts() == {"high": 1, "medium": 1, "low": 1}


def test_merge_empty_returns_low() -> None:
    """无风险项时整体等级为 low（"没发现问题"≠"未知"）。"""
    out = Merger().merge([], [])
    assert out.overall_risk is RiskLevel.LOW
    assert out.counts() == {"high": 0, "medium": 0, "low": 0}


def test_merge_sets_sequential_seq() -> None:
    """合并后 seq 必须连续，供落库排序。"""
    out = Merger().merge([_rule_hit(RiskLevel.HIGH, 0), _rule_hit(RiskLevel.LOW, 1)], [])
    assert [r.seq for r in out.risks] == [0, 1]


# ==================== 防幻觉闸门 ====================

def test_gate_anchors_by_quote() -> None:
    """有精确引用 → 锚定成功，unanchored=0。"""
    text = "乙方逾期交货的，应承担甲方全部损失。"
    doc = _make_doc(text)
    clause = _make_clause(text)
    builder = AnchorBuilder(doc)
    from app.services.review.merger import MergedEvidence, MergedRisk

    risk = MergedRisk(
        title="赔偿责任无上限", risk_level=RiskLevel.HIGH, category="liability",
        reason="r", suggestion=None, legal_basis=None, merged_by=MergedBy.RULE,
        clause_index=0, quote="乙方逾期交货的", is_global=False,
        evidences=[MergedEvidence(evidence_type="rule", title="规则", detail="命中")],
    )
    outcome = HallucinationGate(builder, [clause], None).apply([risk])
    assert outcome.unanchored_count == 0
    assert outcome.items[0].anchors[0].level is AnchorLevel.EXACT


def test_gate_falls_back_to_clause_and_marks_unanchored() -> None:
    """引用无法锚定但知道条款位置 → 降级为段落级锚点。"""
    doc = _make_doc("第一条 违约责任：违约方赔偿全部损失。")
    clause = _make_clause("第一条 违约责任：违约方赔偿全部损失。")
    from app.services.review.merger import MergedEvidence, MergedRisk

    risk = MergedRisk(
        title="赔偿无上限", risk_level=RiskLevel.HIGH, category="liability",
        reason="r", suggestion=None, legal_basis=None, merged_by=MergedBy.LLM,
        clause_index=0, quote="合同中并不存在的编造内容甲乙丙丁",
        is_global=False,
        evidences=[MergedEvidence(evidence_type="llm", title="AI", detail="判定")],
    )
    outcome = HallucinationGate(AnchorBuilder(doc), [clause], None).apply([risk])
    assert outcome.items[0].anchors[0].level is AnchorLevel.PARAGRAPH
    assert outcome.unanchored_count == 0


def test_gate_marks_unanchored_when_nothing_locatable() -> None:
    """既无引用又无条款位置 → unanchored=1（显式标记，不丢弃）。"""
    from app.services.review.merger import MergedEvidence, MergedRisk

    risk = MergedRisk(
        title="编造风险", risk_level=RiskLevel.HIGH, category="other",
        reason="r", suggestion=None, legal_basis=None, merged_by=MergedBy.LLM,
        clause_index=None, quote=None, is_global=True,
        evidences=[MergedEvidence(evidence_type="llm", title="AI", detail="判定")],
    )
    outcome = HallucinationGate(AnchorBuilder(_make_doc("正文")), [], None).apply([risk])
    assert outcome.unanchored_count == 1
    assert outcome.items[0].unanchored is True
    assert outcome.items[0].anchors == []


def test_gate_flags_unverified_legal_basis() -> None:
    """法条不在知识库（db=None）→ 依据 need_review=1，提示人工复核。"""
    from app.services.review.merger import MergedEvidence, MergedRisk

    risk = MergedRisk(
        title="管辖违规", risk_level=RiskLevel.HIGH, category="jurisdiction",
        reason="r", suggestion=None, legal_basis="《民法典》第五百零七条",
        merged_by=MergedBy.LLM, clause_index=None, quote=None, is_global=True,
        evidences=[MergedEvidence(evidence_type="llm", title="AI", detail="判定")],
    )
    outcome = HallucinationGate(AnchorBuilder(_make_doc("正文")), [], None).apply([risk])
    assert outcome.need_review_count == 1
    assert risk.evidences[0].need_review is True


# ==================== 流水线端到端 ====================

def _wps_available() -> bool:
    try:
        from app.services.parsing.docx_converter import WpsComConverter
        return WpsComConverter().is_available()
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="示例合同不存在")
@pytest.mark.skipif(not _wps_available(), reason="WPS COM 不可用（DOCX 分页依赖它）")
def test_pipeline_end_to_end(db: Session) -> None:
    """端到端：DOCX → 解析 → 切分 → 双引擎 → 合并 → 闸门 → 落库 → completed。"""
    from app.core.minio_client import get_minio
    from app.core.config import settings
    from app.services.llm import MockProvider
    from app.workers.pipeline import Pipeline

    h = hashlib.sha256(SAMPLE_DOCX.read_bytes()).hexdigest()
    c = Contract(
        title="设备采购合同（高风险样本）", business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.DOCX.value, file_object_key="test/original.docx",
        file_name=SAMPLE_DOCX.name, file_size=SAMPLE_DOCX.stat().st_size,
        file_hash=h, source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()

    result = Pipeline(db, t, provider=MockProvider()).run(SAMPLE_DOCX)

    assert result.status == TaskStatus.COMPLETED.value
    assert result.overall_risk == RiskLevel.HIGH.value
    assert result.conclusion == "reject"
    assert result.risk_count >= 1

    db.refresh(t)
    db.refresh(c)
    # 状态与冗余计数
    assert t.status == TaskStatus.COMPLETED.value
    assert t.conclusion == "reject"
    assert t.high_risk_count == result.counts["high"]
    assert t.medium_risk_count == result.counts["medium"]
    assert t.summary, "完成的任务必须有综合摘要"

    # 元数据回填到 contract（缺陷 2/3 回归）
    assert c.amount == Decimal("2299000.00"), "金额应回填到 contract.amount"
    assert c.contract_no == "CG-2026-0912"
    assert c.counterparty_name and c.counterparty_code
    assert c.currency == "CNY"

    # 转换后 PDF 真实上传到 MinIO（缺陷 1 回归）
    assert c.pdf_object_key
    st = get_minio().stat_object(settings.minio_bucket_contracts, c.pdf_object_key)
    assert st.size > 0, "pdf_object_key 指向的对象必须真实存在"

    # 风险项、锚点、依据
    risks = db.execute(
        select(RiskItem).where(RiskItem.contract_id == c.id)
    ).scalars().all()
    assert len(risks) == result.risk_count
    for r in risks:
        assert r.reason
        if r.unanchored == 0:
            n = db.execute(
                select(func.count()).select_from(Anchor).where(
                    Anchor.owner_type == "risk_item", Anchor.owner_id == r.id
                )
            ).scalar()
            assert n >= 1, f"未锚定的风险项 {r.title!r} 必须至少有 1 个锚点"

    # 双来源留痕：merged_by=both 必须有 rule + llm 两类依据
    for r in risks:
        ev_types = {
            e.evidence_type for e in db.execute(
                select(RiskEvidence).where(RiskEvidence.risk_item_id == r.id)
            ).scalars()
        }
        if r.merged_by == MergedBy.BOTH.value:
            assert ev_types == {"rule", "llm"}
        else:
            assert ev_types == {r.merged_by}

    # LLM 调用留痕
    from app.models import LlmCallLog
    logs = db.execute(
        select(LlmCallLog).where(LlmCallLog.task_id == t.id)
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].status == "success"


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="示例合同不存在")
@pytest.mark.skipif(not _wps_available(), reason="WPS COM 不可用")
def test_pipeline_rerun_is_idempotent(db: Session) -> None:
    """重试链路：cleanup → 重跑，产物不叠加、attempt 递增、人工批注保留。"""
    from app.services.llm import MockProvider
    from app.workers.pipeline import Pipeline

    h = hashlib.sha256(SAMPLE_DOCX.read_bytes()).hexdigest()
    c = Contract(
        title="设备采购合同（重试用例）", business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.DOCX.value, file_object_key="test/original.docx",
        file_name=SAMPLE_DOCX.name, file_size=SAMPLE_DOCX.stat().st_size,
        file_hash=h, source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()

    Pipeline(db, t, provider=MockProvider()).run(SAMPLE_DOCX)
    first_count = db.execute(
        select(func.count()).select_from(RiskItem).where(RiskItem.contract_id == c.id)
    ).scalar()
    risk0 = db.execute(
        select(RiskItem).where(RiskItem.contract_id == c.id).order_by(RiskItem.seq)
    ).scalars().first()
    db.add(Annotation(contract_id=c.id, risk_item_id=risk0.id, author="张三", content="同意"))
    db.commit()

    # 模拟解析受阻 → 人工重试
    db.execute(text("UPDATE review_task SET status='blocked', blocked_reason='timeout' WHERE id=:i"),
               {"i": t.id})
    db.commit()
    # 原始 SQL 绕过了 ORM 身份映射，必须 expire 才能让 Pipeline 读到 blocked 状态
    # （否则它读到过期的 completed，触发 completed -> parsing 非法流转）
    db.expire_all()
    t = db.get(ReviewTask, t.id)
    sm.cleanup_for_retry(db, t)
    db.commit()
    Pipeline(db, t, provider=MockProvider()).run(SAMPLE_DOCX)

    after = db.execute(
        select(func.count()).select_from(RiskItem).where(RiskItem.contract_id == c.id)
    ).scalar()
    assert after == first_count, "重试后风险项不应叠加"

    attempts = sorted(r.attempt for r in db.execute(
        select(ParseResult).where(ParseResult.contract_id == c.id)
    ).scalars())
    assert attempts == [1, 2], "parse_result 应保留历史并递增 attempt"

    ann = db.execute(select(Annotation).where(Annotation.contract_id == c.id)).scalars().one()
    assert ann.content == "同意", "人工批注必须保留"

    # 无孤儿锚点
    orphans = db.execute(text("""
        SELECT COUNT(*) FROM anchor a
        LEFT JOIN risk_item ri ON a.owner_type='risk_item' AND a.owner_id=ri.id
        LEFT JOIN contract_metadata cm ON a.owner_type='contract_metadata' AND a.owner_id=cm.id
        WHERE ri.id IS NULL AND cm.id IS NULL
    """)).scalar()
    assert orphans == 0


@pytest.mark.skipif(not SAMPLE_DOCX.exists(), reason="示例合同不存在")
@pytest.mark.skipif(not _wps_available(), reason="WPS COM 不可用")
def test_pipeline_accepts_task_already_in_parsing(db: Session) -> None:
    """重试链路回归：任务已被置为 parsing 时，Pipeline 不得再流转一次。

    `POST /api/tasks/{id}/retry` 与批量重试都**先落库置 parsing 再起后台线程**
    （避免线程读到未提交的旧状态），因此 Pipeline.run 拿到的就是 parsing 任务。
    若它无条件再 `transition(PARSING)`，会撞上 parsing -> parsing 非法流转，
    任务永久卡在 parsing——这正是批次 6 实测到的缺陷。
    """
    from app.services.llm import MockProvider
    from app.workers.pipeline import Pipeline

    h = hashlib.sha256(SAMPLE_DOCX.read_bytes()).hexdigest()
    c = Contract(
        title="重试置 parsing 用例", business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.DOCX.value, file_object_key="test/original.docx",
        file_name=SAMPLE_DOCX.name, file_size=SAMPLE_DOCX.stat().st_size,
        file_hash=h, source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    # 模拟重试接口的状态：已经是 parsing
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PARSING.value)
    db.add(t)
    db.commit()

    result = Pipeline(db, t, provider=MockProvider()).run(SAMPLE_DOCX)

    assert result.status == TaskStatus.COMPLETED.value, (
        "已处于 parsing 的任务必须能继续跑完，而不是被 parsing -> parsing 卡死"
    )
    db.refresh(t)
    assert t.status == TaskStatus.COMPLETED.value


# ==================== 元数据提取（批次 5）====================

def test_extract_effective_condition() -> None:
    """生效条件必须被提取（PRD 2.4.4 点名的核心元数据）。

    三种常见写法都要覆盖：自…之日起生效 / 自…后生效 / 经…后生效。
    """
    for text, expected in [
        ("本合同自双方签字盖章之日起生效。", "双方签字盖章"),
        ("本合同自双方签字并加盖公章后生效。", "双方签字并加盖公章"),
        ("本合同经双方授权代表签署后生效。", "双方授权代表签署"),
    ]:
        metas = {m.meta_key: m for m in extract_metadata(_make_doc(text))}
        assert MetadataKey.EFFECTIVE_CONDITION in metas, f"未提取到生效条件: {text}"
        assert metas[MetadataKey.EFFECTIVE_CONDITION].meta_value == expected


def test_extract_effective_condition_does_not_false_positive() -> None:
    """「合同期限：自…起至…止」不是生效条件，不得误捕。

    回归缺陷：早期正则只要求「自…起」，把履约期限也当成了生效条件。
    """
    text = "合同期限：自2026 年10 月1 日起至2027 年9 月30 日止。"
    metas = {m.meta_key for m in extract_metadata(_make_doc(text))}
    assert MetadataKey.EFFECTIVE_CONDITION not in metas
    # 期限本身仍要正常提取
    assert MetadataKey.TERM in metas


def test_metadata_carries_char_span() -> None:
    """元数据要带全文字符区间，且区间精确指向取值本身。

    区间是元数据锚点的唯一来源，错一位就会框错字。
    """
    text = "合同编号：CG-2026-0912\n甲方（采购方）：某某科技有限公司"
    metas = {m.meta_key: m for m in extract_metadata(_make_doc(text))}

    m = metas[MetadataKey.CONTRACT_NO]
    assert m.char_start is not None and m.char_end is not None
    assert text[m.char_start:m.char_end] == "CG-2026-0912"

    a = metas[MetadataKey.PARTY_A_NAME]
    assert text[a.char_start:a.char_end] == "某某科技有限公司"


def test_metadata_span_distinguishes_duplicate_values() -> None:
    """同一取值在原文出现多次时，区间必须各指其位。

    回归缺陷：若用 `locate(取值)` 反查会锚到**第一处**，
    甲乙双方同名时后一个锚点就错位了。
    """
    text = "甲方：某某科技有限公司\n乙方：某某科技有限公司"
    metas = {m.meta_key: m for m in extract_metadata(_make_doc(text))}
    a = metas[MetadataKey.PARTY_A_NAME]
    b = metas[MetadataKey.PARTY_B_NAME]
    assert a.char_start != b.char_start, "同名主体的区间不应重合"
    assert text[a.char_start:a.char_end] == "某某科技有限公司"
    assert text[b.char_start:b.char_end] == "某某科技有限公司"
    assert b.char_start > a.char_start


def test_locate_span_rejects_invalid_range() -> None:
    """区间非法时返回 None，绝不产出越界锚点。"""
    doc = _make_doc("短文本。")
    builder = AnchorBuilder(doc)
    assert builder.locate_span(0, 0) is None          # 空区间
    assert builder.locate_span(5, 2) is None          # 倒置
    assert builder.locate_span(-1, 3) is None         # 负值
    assert builder.locate_span(0, 9999) is None       # 越界
    ok = builder.locate_span(0, 3)
    assert ok is not None and ok.anchored
    assert ok.level is AnchorLevel.EXACT
