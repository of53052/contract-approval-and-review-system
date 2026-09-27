"""审查报告生成与导出。

设计依据：docs/architecture.md §7.1（存储映射）、§14（报告要求）；
PRD 2.4.8（报告结构）。

两个职责：
1. **生成 Markdown 文本**：纯函数，输入是数据库里的实体，输出是字符串。
   同一份数据永远生成同一份文本，便于回归比对。
2. **导出到 MinIO**：把文本落对象存储并登记 `export_record`。

**阶段一不做 PDF 精排**（architecture.md §18.1）：`format` 只支持 markdown。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.minio_client import get_minio, path_report
from app.models import (
    Anchor,
    Clause,
    Contract,
    ContractMetadata,
    ExportRecord,
    ReviewTask,
    RiskEvidence,
    RiskItem,
)
from app.models.enums import RiskLevel

logger = logging.getLogger(__name__)

#: 风险等级的中文标签与排序权重
_LEVEL_LABEL = {
    RiskLevel.HIGH.value: "高风险",
    RiskLevel.MEDIUM.value: "中风险",
    RiskLevel.LOW.value: "低风险",
}
_LEVEL_ORDER = {RiskLevel.HIGH.value: 0, RiskLevel.MEDIUM.value: 1, RiskLevel.LOW.value: 2}
_LEVEL_ICON = {RiskLevel.HIGH.value: "🔴", RiskLevel.MEDIUM.value: "🟠", RiskLevel.LOW.value: "🟡"}

#: 元数据键的中文标签（报告"合同基本信息"用）
_META_LABEL = {
    "contract_no": "合同编号",
    "party_a_name": "甲方",
    "party_b_name": "乙方",
    "party_a_credit_code": "甲方统一社会信用代码",
    "party_b_credit_code": "乙方统一社会信用代码",
    "amount": "合同金额",
    "currency": "币种",
    "term": "履约期限",
    "effective_condition": "生效条件",
    "sign_date": "签订日期",
    "sign_place": "签订地点",
}

_CONCLUSION_LABEL = {
    "pass": "通过",
    "rectify": "建议整改",
    "reject": "建议拒绝",
}

_ANCHOR_LEVEL_LABEL = {
    "exact": "精确",
    "fuzzy": "模糊",
    "paragraph": "段落级",
}


@dataclass
class ReportBundle:
    """一份报告的全部数据。一次性查完，避免生成时反复打库。"""

    contract: Contract
    task: ReviewTask
    risks: list[RiskItem] = field(default_factory=list)
    #: risk_item_id -> 锚点列表
    anchors: dict[int, list[Anchor]] = field(default_factory=dict)
    #: risk_item_id -> 依据列表
    evidences: dict[int, list[RiskEvidence]] = field(default_factory=dict)
    metadata: list[ContractMetadata] = field(default_factory=list)
    #: clause_id -> Clause
    clauses: dict[int, Clause] = field(default_factory=dict)


def load_bundle(db: Session, contract_id: int) -> ReportBundle | None:
    """载入生成报告所需的全部数据。合同不存在时返回 None。"""
    contract = db.get(Contract, contract_id)
    if contract is None:
        return None
    task = db.execute(
        select(ReviewTask).where(ReviewTask.contract_id == contract_id)
    ).scalar_one_or_none()
    if task is None:
        return None

    risks = list(db.execute(
        select(RiskItem).where(RiskItem.contract_id == contract_id).order_by(RiskItem.seq)
    ).scalars())

    bundle = ReportBundle(contract=contract, task=task, risks=risks)
    bundle.metadata = list(db.execute(
        select(ContractMetadata).where(ContractMetadata.contract_id == contract_id)
    ).scalars())

    clause_rows = list(db.execute(
        select(Clause).where(Clause.contract_id == contract_id)
    ).scalars())
    bundle.clauses = {c.id: c for c in clause_rows}

    if risks:
        risk_ids = [r.id for r in risks]
        for a in db.execute(
            select(Anchor).where(
                Anchor.owner_type == "risk_item", Anchor.owner_id.in_(risk_ids)
            ).order_by(Anchor.owner_id, Anchor.seq)
        ).scalars():
            bundle.anchors.setdefault(a.owner_id, []).append(a)
        for e in db.execute(
            select(RiskEvidence).where(RiskEvidence.risk_item_id.in_(risk_ids))
        ).scalars():
            bundle.evidences.setdefault(e.risk_item_id, []).append(e)

    return bundle


# ==================== 渲染 ====================

def render_markdown(bundle: ReportBundle) -> str:
    """把报告数据渲染成 Markdown。

    结构对齐 PRD 2.4.8：合同基本信息 → 综合审查结论 → 风险清单明细 → 附录。
    """
    c, t = bundle.contract, bundle.task
    lines: list[str] = []

    lines.append(f"# 合同审查报告：{c.title}")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}　|　"
                 f"审查任务 #{t.id}　|　合同 #{c.id}")
    lines.append("")

    # ---------- 合同基本信息 ----------
    lines.append("## 一、合同基本信息")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 合同名称 | {c.title} |")
    lines.append(f"| 合同编号 | {c.contract_no or _meta_value(bundle, 'contract_no') or '—'} |")
    lines.append(f"| 送审部门 | {c.applicant_dept or '—'} |")
    lines.append(f"| 申请人 | {c.applicant or '—'} |")
    lines.append(f"| 相对方 | {c.counterparty_name or _meta_value(bundle, 'party_b_name') or '—'} |")
    lines.append(f"| 相对方信用代码 | {c.counterparty_code or _meta_value(bundle, 'party_b_credit_code') or '—'} |")
    amount = f"{c.amount:,.2f} {c.currency or ''}".strip() if c.amount is not None else "—"
    lines.append(f"| 合同金额 | {amount} |")
    lines.append(f"| 履约期限 | {_meta_value(bundle, 'term') or '—'} |")
    lines.append(f"| 业务类型 | {c.business_type} |")
    lines.append(f"| 文件名 | {c.file_name} |")
    lines.append("")

    # ---------- 综合审查结论 ----------
    lines.append("## 二、综合审查结论")
    lines.append("")
    overall = t.overall_risk or "—"
    lines.append(f"- **总风险等级**：{_LEVEL_ICON.get(overall, '')} "
                 f"{_LEVEL_LABEL.get(overall, overall)}")
    lines.append(f"- **审查结论**：{_CONCLUSION_LABEL.get(t.conclusion or '', t.conclusion or '—')}")
    counts = f"高风险 {t.high_risk_count} 项、中风险 {t.medium_risk_count} 项、低风险 {t.low_risk_count} 项"
    lines.append(f"- **风险分布**：{counts}")
    if t.summary:
        lines.append(f"- **核心摘要**：{t.summary}")
    lines.append("")

    # ---------- 风险清单明细 ----------
    lines.append("## 三、风险清单明细")
    lines.append("")
    if not bundle.risks:
        lines.append("未发现风险项。")
        lines.append("")
    for idx, r in enumerate(bundle.risks, start=1):
        icon = _LEVEL_ICON.get(r.risk_level, "")
        lines.append(f"### {idx}. {icon} {r.title}")
        lines.append("")
        lines.append(f"- **风险等级**：{_LEVEL_LABEL.get(r.risk_level, r.risk_level)}")
        lines.append(f"- **风险分类**：{r.category}")
        lines.append(f"- **定级来源**：{_merged_by_label(r.merged_by)}")

        # 涉及条款与原文定位
        clause = bundle.clauses.get(r.clause_id) if r.clause_id else None
        if clause is not None:
            lines.append(f"- **涉及条款**：{clause.clause_no or ''} {clause.title or ''}".rstrip())
        anchor_desc = _describe_anchor(bundle.anchors.get(r.id, []), r.unanchored)
        lines.append(f"- **原文定位**：{anchor_desc}")

        lines.append("")
        lines.append(f"**风险成因**：{r.reason}")
        lines.append("")
        if r.legal_basis:
            lines.append(f"**法律依据**：{r.legal_basis}")
            lines.append("")
        suggestion = r.suggestion_edited or r.suggestion
        if suggestion:
            tag = "推荐修改条款（法务已编辑）" if r.suggestion_edited else "推荐修改条款"
            lines.append(f"**{tag}**：")
            lines.append("")
            lines.append("> " + suggestion.replace("\n", "\n> "))
            lines.append("")
        if r.adopted:
            lines.append("> ✅ 法务已采纳该建议")
            lines.append("")

        # 依据链（双来源留痕）
        evs = bundle.evidences.get(r.id, [])
        if evs:
            lines.append("<details><summary>依据链（点击展开）</summary>")
            lines.append("")
            for e in evs:
                src = "规则命中" if e.evidence_type == "rule" else "AI 研判"
                flag = "　⚠️ 待人工复核" if e.need_review else ""
                title = f"「{e.title}」" if e.title else ""
                lines.append(f"- **[{src}]{flag}** {title}{e.detail}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # ---------- 附录 ----------
    lines.append("## 四、附录")
    lines.append("")
    lines.append(f"- 解析方式：`{_parse_method(bundle)}`")
    lines.append(f"- 解析页数：{t.total_pages or '—'}")
    if t.parse_duration_ms is not None:
        lines.append(f"- 解析耗时：{t.parse_duration_ms} ms")
    if t.review_duration_ms is not None:
        lines.append(f"- 审查耗时：{t.review_duration_ms} ms")
    lines.append(f"- 任务状态：{t.status}（version={t.version}）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> ⚠️ 页码与位置基于解析时实际生效的排版引擎。"
                 "DOCX 经 WPS 转换，页码可能与 Word 打开时不一致。")
    lines.append("> 本报告由合同审查系统自动生成，AI 产出的法律依据需人工复核。")
    lines.append("")

    return "\n".join(lines)


def _meta_value(bundle: ReportBundle, key: str) -> str | None:
    for m in bundle.metadata:
        if m.meta_key == key:
            return m.meta_value
    return None


def _merged_by_label(merged_by: str) -> str:
    return {
        "rule": "规则命中",
        "llm": "AI 研判",
        "both": "规则 + AI 双重确认",
        "manual": "人工添加",
    }.get(merged_by, merged_by)


def _describe_anchor(anchors: list[Anchor], unanchored: int) -> str:
    """把锚点列表描述成可读文本。"""
    if unanchored or not anchors:
        return "⚠️ 无法定位到原文，需人工核查"
    parts: list[str] = []
    for a in anchors:
        level = _ANCHOR_LEVEL_LABEL.get(a.anchor_level, a.anchor_level)
        quote = f"「{a.quote_text}」" if a.quote_text else ""
        parts.append(f"第 {a.page_no} 页（{level}）{quote}")
    return "；".join(parts)


def _parse_method(bundle: ReportBundle) -> str:
    """取最近一次解析的 parse_method。

    报告需要解释页码来源，因此必须读实际的解析记录而非任务字段。
    """
    # bundle 里没带 parse_result，用 contract 的关系读；已在会话内的对象不会额外打库
    prs = sorted(bundle.contract.parse_results, key=lambda p: p.attempt)
    return prs[-1].parse_method if prs else "—"


# ==================== 导出 ====================

@dataclass
class ExportResult:
    """导出结果。"""

    record_id: int
    object_key: str
    file_size: int
    format: str


def export_markdown(
    db: Session, bundle: ReportBundle, *, created_by: str | None = None
) -> ExportResult:
    """生成 Markdown 报告并导出到 MinIO，登记 `export_record`。

    **先上传成功再落库**：与转换后 PDF 同样的顺序，避免出现
    "库里有记录、MinIO 里没对象"的悬空引用。
    """
    text = render_markdown(bundle)
    data = text.encode("utf-8")
    key = path_report(bundle.contract.id, datetime.now().strftime("%Y%m%d_%H%M%S"), "md")

    client = get_minio()
    client.put_object(
        settings.minio_bucket_reports,
        key,
        BytesIO(data),
        length=len(data),
        content_type="text/markdown; charset=utf-8",
    )

    record = ExportRecord(
        contract_id=bundle.contract.id,
        format="markdown",
        file_object_key=key,
        file_size=len(data),
        created_by=created_by,
    )
    db.add(record)
    db.flush()

    logger.info(
        "报告已导出: contract=%s key=%s/%s（%s 字节）",
        bundle.contract.id, settings.minio_bucket_reports, key, len(data),
    )
    return ExportResult(
        record_id=record.id, object_key=key, file_size=len(data), format="markdown"
    )
