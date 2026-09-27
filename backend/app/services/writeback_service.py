"""审批回写服务。

设计依据：docs/architecture.md §7.2（回写状态）、§11.2（协同改写流程）；
docs/data-model.md §5.11（writeback_log）、§6.3。

**回写状态机**（与 `review_task.writeback_status` 同步）：
    not_written → writing → success
                        └→ failed（可重试）

**幂等**（D10 + §13.3）：
1. 发起前先落一条 `status='writing'` 记录，防进程崩溃丢状态
2. 同一合同已有 `success` 记录时**直接返回**，不重复回写（C6 检查兜底）
3. 传给审批系统的 `idempotency_key` 由 contract_id + 内容哈希构成，
   服务端也做幂等——两层都有才能防住"请求已到达但响应丢失"

**回写内容**：格式化 Markdown，含风险总评、重点关注事项、法务批注
（PRD 2.4.8）。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Annotation, Contract, ReviewTask, RiskItem, WritebackLog
from app.models.enums import RiskLevel, WritebackStatus
from app.services.approval import ApprovalError, ApprovalSystemAdapter, get_adapter
from app.services.report_service import ReportBundle, _LEVEL_LABEL, _merged_by_label

logger = logging.getLogger(__name__)


class WritebackBlocked(Exception):
    """回写被拒（前置条件不满足）。**不可重试**，需先修数据。"""


@dataclass
class WritebackResult:
    """回写结果摘要。"""

    contract_id: int
    status: str
    log_id: int
    comment_id: str | None = None
    deduplicated: bool = False
    http_status: int | None = None
    duration_ms: int = 0
    error_detail: str | None = None


# ==================== 文本构建 ====================

def build_comment(bundle: ReportBundle, annotations: list[Annotation]) -> str:
    """构建回写用的 Markdown 评论文本。

    结构（PRD 2.4.8）：风险总评 → 重点关注事项 → 法务批注。
    **刻意比完整报告短**——评论区是给审批人看的摘要，不是报告全文。
    """
    t = bundle.task
    lines: list[str] = []

    lines.append(f"## 📋 合同审查意见：{bundle.contract.title}")
    lines.append("")

    overall = t.overall_risk or "—"
    lines.append(f"**总风险等级**：{_LEVEL_LABEL.get(overall, overall)}　|　"
                 f"**审查结论**：{_conclusion_label(t.conclusion)}")
    lines.append("")
    lines.append(
        f"风险分布：高风险 {t.high_risk_count} 项、"
        f"中风险 {t.medium_risk_count} 项、低风险 {t.low_risk_count} 项"
    )
    lines.append("")
    if t.summary:
        lines.append(f"> {t.summary}")
        lines.append("")

    # 只列高/中风险，且只列标题与建议——低风险不占评论区篇幅
    focus = [r for r in bundle.risks if r.risk_level in (RiskLevel.HIGH.value, RiskLevel.MEDIUM.value)]
    if focus:
        lines.append("### 需重点关注事项")
        lines.append("")
        for i, r in enumerate(focus, start=1):
            icon = "🔴" if r.risk_level == RiskLevel.HIGH.value else "🟠"
            lines.append(f"{i}. {icon} **{r.title}**（{_LEVEL_LABEL.get(r.risk_level, r.risk_level)}，"
                         f"{_merged_by_label(r.merged_by)}）")
            lines.append(f"   - 成因：{r.reason}")
            suggestion = r.suggestion_edited or r.suggestion
            if suggestion:
                first_line = suggestion.strip().splitlines()[0]
                lines.append(f"   - 建议：{first_line}")
        lines.append("")

    if annotations:
        lines.append("### 法务批注")
        lines.append("")
        for a in annotations:
            role = f"（{a.author_role}）" if a.author_role else ""
            lines.append(f"- **{a.author}**{role}：{a.content}")
        lines.append("")

    lines.append("---")
    lines.append("*本意见由合同审查系统自动生成，AI 产出的法律依据需人工复核。*")
    return "\n".join(lines)


def _conclusion_label(conclusion: str | None) -> str:
    return {"pass": "通过", "rectify": "建议整改", "reject": "建议拒绝"}.get(
        conclusion or "", conclusion or "—"
    )


def make_idempotency_key(contract_id: int, content: str) -> str:
    """构造幂等键：合同 + 内容哈希。

    同一合同若审查结果变了（重新审查后风险项不同），内容哈希会变，
    允许再写一条新评论——这是**期望行为**：结论变了就该通知审批人。
    相同内容重复触发则命中幂等，不产生重复评论。
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"cr-wb-{contract_id}-{digest}"


# ==================== 回写主流程 ====================

def write_back(
    db: Session,
    bundle: ReportBundle,
    *,
    author: str = "合同审查系统",
    adapter: ApprovalSystemAdapter | None = None,
    force: bool = False,
) -> WritebackResult:
    """把审查意见回写到审批系统。

    `force=True` 时跳过"已有 success 记录"的检查（演示中重新回写用）。

    失败**不抛异常**（除非前置条件不满足）：把状态落成 `failed` 并返回，
    调用方据此提示用户重试。这与解析阶段的"可恢复错误就近处理"一致。
    """
    contract = bundle.contract
    task = bundle.task
    t0 = time.perf_counter()

    # ---------- 前置条件 ----------
    if task.status != "completed":
        raise WritebackBlocked(
            f"任务未完成，不允许回写（当前状态 {task.status}）"
        )
    if not contract.external_id:
        raise WritebackBlocked(
            "合同未关联审批单号（external_id 为空），无法回写"
        )

    # ---------- 幂等：已有成功记录则直接返回 ----------
    if not force:
        existing = db.execute(
            select(WritebackLog).where(
                WritebackLog.contract_id == contract.id,
                WritebackLog.status == WritebackStatus.SUCCESS.value,
            ).order_by(WritebackLog.created_at.desc())
        ).scalars().first()
        if existing is not None:
            logger.info("合同 %s 已有成功回写记录，跳过（幂等）", contract.id)
            return WritebackResult(
                contract_id=contract.id,
                status=WritebackStatus.SUCCESS.value,
                log_id=existing.id,
                comment_id=None,
                deduplicated=True,
                http_status=existing.http_status,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

    annotations = list(db.execute(
        select(Annotation).where(Annotation.contract_id == contract.id).order_by(Annotation.id)
    ).scalars())
    content = build_comment(bundle, annotations)
    idem_key = make_idempotency_key(contract.id, content)

    # ---------- 先落 writing 记录（防崩溃丢状态）----------
    log = WritebackLog(
        contract_id=contract.id,
        status=WritebackStatus.WRITING.value,
        payload=content,
        retry_count=0,
    )
    db.add(log)
    task.writeback_status = WritebackStatus.WRITING.value
    db.commit()  # 必须先提交：若后续进程崩溃，这条记录是唯一线索

    # ---------- 调用审批系统 ----------
    adapter = adapter or get_adapter()
    try:
        result = adapter.write_comment(
            contract.external_id, author, content, idempotency_key=idem_key
        )
    except ApprovalError as exc:
        duration = int((time.perf_counter() - t0) * 1000)
        log.status = WritebackStatus.FAILED.value
        log.error_detail = str(exc)[:2000]
        log.http_status = exc.http_status
        log.retry_count = log.retry_count + 1
        log.duration_ms = duration
        task.writeback_status = WritebackStatus.FAILED.value
        db.commit()
        logger.warning("合同 %s 回写失败: %s", contract.id, exc)
        return WritebackResult(
            contract_id=contract.id,
            status=WritebackStatus.FAILED.value,
            log_id=log.id,
            http_status=exc.http_status,
            duration_ms=duration,
            error_detail=str(exc)[:500],
        )

    duration = int((time.perf_counter() - t0) * 1000)
    log.status = WritebackStatus.SUCCESS.value
    log.response = f"comment_id={result.comment_id} deduplicated={result.deduplicated}"
    log.http_status = result.http_status
    log.duration_ms = duration
    task.writeback_status = WritebackStatus.SUCCESS.value
    db.commit()

    logger.info(
        "合同 %s 回写成功: comment_id=%s dedup=%s（%s ms）",
        contract.id, result.comment_id, result.deduplicated, duration,
    )
    return WritebackResult(
        contract_id=contract.id,
        status=WritebackStatus.SUCCESS.value,
        log_id=log.id,
        comment_id=result.comment_id,
        deduplicated=result.deduplicated,
        http_status=result.http_status,
        duration_ms=duration,
    )


def get_writeback_status(db: Session, contract_id: int) -> dict:
    """查询回写状态（供前端轮询）。"""
    task = db.execute(
        select(ReviewTask).where(ReviewTask.contract_id == contract_id)
    ).scalar_one_or_none()
    latest = db.execute(
        select(WritebackLog)
        .where(WritebackLog.contract_id == contract_id)
        .order_by(WritebackLog.created_at.desc())
    ).scalars().first()
    return {
        "writeback_status": task.writeback_status if task else "not_written",
        "latest_log_id": latest.id if latest else None,
        "latest_status": latest.status if latest else None,
        "error_detail": latest.error_detail if latest else None,
        "created_at": latest.created_at.isoformat() if latest else None,
    }
