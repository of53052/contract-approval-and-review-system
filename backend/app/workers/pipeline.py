"""审查流水线：解析 → 切分 → 双引擎审查 → 合并 → 闸门 → 落库。

设计依据：docs/architecture.md §13.2（任务执行流程）、§8（审查引擎）。

**进程内后台任务**（FastAPI BackgroundTasks），不用 Celery（§13.1）。
任务表设计成可迁移的，阶段二若需批量审查再迁。

**幂等性**（§13.3）：
- 重试时先清理旧产物
- 文件去重靠 `file_hash`，相同文件不重复解析
- 进度上报到 Redis，前端可轮询

**错误处理**：可恢复的错误（加密、空白、转换器不可用）转 `blocked` 并记录原因；
不可恢复的错误（数据库异常、代码 bug）向上抛，**不静默吞没**。
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.minio_client import get_minio, path_converted_pdf
from app.core.redis_client import get_redis, key_task_progress, TTL_TASK_PROGRESS
from app.models import (
    Anchor,
    Clause,
    Contract,
    ContractMetadata,
    ParseResult,
    ReviewTask,
    RiskEvidence,
    RiskItem,
    Rule,
    RuleTemplate,
    SubjectBlacklist,
)
from app.models.enums import (
    AnchorLevel,
    AnchorSource,
    BusinessType,
    MergedBy,
    ParseStatus,
    ReviewConclusion,
    RiskLevel,
    TaskStatus,
)
from app.services.llm import LLMProvider, get_provider
from app.services.parsing.anchor_builder import AnchorBuilder
from app.services.parsing.dispatcher import ParseBlocked, parse_document
from app.services.review.clause_splitter import split_document
from app.services.review.global_checker import HallucinationGate, check_global
from app.services.review.llm_reviewer import LlmReviewer
from app.services.review.merger import Merger
from app.services.review.rule_engine import RuleEngine, RuleEngineInput
from app.workers import state_machine as sm

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """流水线执行结果摘要。"""

    task_id: int
    status: str
    overall_risk: str | None = None
    conclusion: str | None = None
    risk_count: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    blocked_reason: str | None = None


class Pipeline:
    """单合同审查流水线。

    一个实例处理一个任务；`run()` 是唯一入口。
    """

    def __init__(
        self,
        db: Session,
        task: ReviewTask,
        *,
        provider: LLMProvider | None = None,
        allow_ocr: bool = False,
    ) -> None:
        self.db = db
        self.task = task
        self.provider = provider
        self.allow_ocr = allow_ocr
        self._t0 = 0.0

    def run(self, source_path: Path) -> PipelineResult:
        """执行完整流水线。"""
        self._t0 = time.perf_counter()
        logger.info("任务 %s 开始执行，源文件 %s", self.task.id, source_path)

        try:
            sm.transition(self.db, self.task, TaskStatus.PARSING)
            self.db.commit()
        except sm.InvalidTransition as exc:
            logger.error("任务 %s 无法进入 parsing: %s", self.task.id, exc)
            return self._fail_result(str(exc))

        # ---------- 阶段 1：解析 ----------
        try:
            outcome = parse_document(source_path, allow_ocr=self.allow_ocr)
        except ParseBlocked as exc:
            self.db.rollback()
            sm.block(self.db, self.task, exc.reason.value, exc.detail)
            self.db.commit()
            logger.warning("任务 %s 解析受阻: %s", self.task.id, exc)
            return self._fail_result(exc.detail, blocked_reason=exc.reason.value)
        except Exception as exc:  # noqa: BLE001 - 不可恢复错误，fail-fast
            self.db.rollback()
            logger.error("任务 %s 解析异常", self.task.id, exc_info=True)
            sm.block(self.db, self.task, "error", f"{type(exc).__name__}: {exc}")
            self.db.commit()
            return self._fail_result(f"{type(exc).__name__}: {exc}", blocked_reason="error")

        self._save_parse_result(outcome)
        sm.record_progress(self.db, self.task, outcome.page_count, outcome.page_count)
        self._report_progress(outcome.page_count, outcome.page_count)
        self.db.commit()

        # ---------- 阶段 2：切分与审查 ----------
        try:
            sm.transition(self.db, self.task, TaskStatus.REVIEWING)
            self.db.commit()
            return self._review(outcome)
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            logger.error("任务 %s 审查阶段异常", self.task.id, exc_info=True)
            sm.block(self.db, self.task, "error", f"{type(exc).__name__}: {exc}")
            self.db.commit()
            return self._fail_result(f"{type(exc).__name__}: {exc}", blocked_reason="error")

    # ==================== 审查阶段 ====================

    def _review(self, outcome) -> PipelineResult:
        """切分 → 规则 → LLM → 合并 → 闸门 → 落库。"""
        review_t0 = time.perf_counter()
        contract = self.db.get(Contract, self.task.contract_id)
        business_type = contract.business_type if contract else BusinessType.PURCHASE.value

        # 1) 切分与元数据
        split = split_document(outcome.doc)
        parse_result = self._latest_parse_result()
        self._save_clauses(split.clauses, parse_result.id)
        self._save_metadata(
            split.metadata, parse_result.id, outcome.build_anchor_builder()
        )
        # 把元数据回填到 contract 冗余字段：大盘页与报告"合同基本信息"直接读它，
        # 避免列表页为了显示金额/编号再关联 contract_metadata（原则 P4）
        self._backfill_contract_fields(contract, split.metadata)
        self.db.flush()

        # 2) 规则引擎
        rules = self._load_rules(business_type)
        blacklist_hits = self._load_blacklist_hits(split.metadata)
        rule_hits = RuleEngine(RuleEngineInput(
            clauses=split.clauses,
            metadata=split.metadata,
            blacklist_hits=blacklist_hits,
            rules=rules,
        )).run()

        # 3) LLM 研判
        provider = self.provider or get_provider()
        llm_t0 = time.perf_counter()
        llm_outcome = LlmReviewer(provider).review(split.clauses)
        llm_ms = int((time.perf_counter() - llm_t0) * 1000)
        sm.log_llm_call(
            self.db,
            task_id=self.task.id,
            provider=provider.name,
            model=getattr(provider, "model", None),
            purpose="clause_review",
            status="success" if not llm_outcome.failures else "failed",
            json_retry_count=llm_outcome.total_retries,
            duration_ms=llm_ms,
            error_detail="; ".join(llm_outcome.failures) or None,
        )
        self.db.flush()

        # 4) 合并（取高不取低）
        merged = Merger().merge(rule_hits, llm_outcome.risks)

        # 5) 必备条款全局校验（弥补分块盲区）
        merged = self._append_global_missing(merged, split, business_type)

        # 6) 防幻觉闸门（锚定 + 法条核验）
        gate = HallucinationGate(outcome.build_anchor_builder(), split.clauses, self.db)
        gated = gate.apply(merged.risks)

        # 7) 落库
        self._save_risks(gated, split.clauses)

        # 8) 汇总
        counts = merged.counts()
        self.task.high_risk_count = counts["high"]
        self.task.medium_risk_count = counts["medium"]
        self.task.low_risk_count = counts["low"]
        self.task.overall_risk = merged.overall_risk.value
        self.task.conclusion = _conclude(merged.overall_risk).value
        self.task.summary = _summarize(merged, gated.unanchored_count)
        self.task.review_duration_ms = int((time.perf_counter() - review_t0) * 1000)

        sm.transition(self.db, self.task, TaskStatus.COMPLETED)
        self.db.commit()

        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        logger.info(
            "任务 %s 完成: %s 项风险（高 %s / 中 %s / 低 %s），整体 %s，耗时 %s ms",
            self.task.id, len(merged.risks), counts["high"], counts["medium"],
            counts["low"], merged.overall_risk.value, duration_ms,
        )
        return PipelineResult(
            task_id=self.task.id,
            status=TaskStatus.COMPLETED.value,
            overall_risk=merged.overall_risk.value,
            conclusion=self.task.conclusion,
            risk_count=len(merged.risks),
            counts=counts,
            duration_ms=duration_ms,
        )

    def _append_global_missing(self, merged, split, business_type: str):
        """把必备条款缺失项追加到风险清单。

        与 `rule` 表中的 presence 规则互补：这里是**不可配置的底线清单**，
        保证即使规则被误停用也不会漏掉必备项。
        """
        from app.services.review.merger import MergedEvidence, MergedRisk

        types = {c.clause_type.value for c in split.clauses}
        # 已被规则覆盖的缺失项不再重复报（避免同一问题两条记录）
        reported = {r.title for r in merged.risks}
        for item in check_global(types, split.metadata, business_type):
            title = f"缺少必备条款：{item['key']}" if item["kind"] == "clause"                 else f"缺少{_meta_label(item['key'])}"
            if title in reported:
                continue
            merged.risks.append(MergedRisk(
                title=title[:255],
                risk_level=RiskLevel(item["risk_level"]),
                category=_meta_category(item["key"]),
                reason=item["reason"],
                suggestion=None,
                legal_basis=None,
                merged_by=MergedBy.RULE,
                clause_index=None,
                quote=None,
                is_global=True,
                evidences=[MergedEvidence(
                    evidence_type="rule",
                    title="必备条款全局校验",
                    detail=item["reason"],
                )],
            ))
        merged.risks.sort(key=lambda r: (
            -{"high": 3, "medium": 2, "low": 1}[r.risk_level.value],
            r.clause_index if r.clause_index is not None else -1,
        ))
        for i, r in enumerate(merged.risks):
            r.seq = i
        return merged

    # ==================== 落库 ====================

    def _save_parse_result(self, outcome) -> ParseResult:
        pr = ParseResult(
            contract_id=self.task.contract_id,
            task_id=self.task.id,
            parse_method=outcome.parse_method.value,
            converter_used=outcome.converter_used,
            converter_fallback_chain=outcome.converter_fallback_chain,
            ocr_engine=outcome.ocr_engine,
            ocr_dpi=outcome.ocr_dpi,
            page_count=outcome.page_count,
            failed_pages=outcome.failed_pages,
            has_text_layer=1 if outcome.has_text_layer else 0,
            status=outcome.status.value,
            avg_confidence=outcome.avg_confidence,
            duration_ms=outcome.duration_ms,
            attempt=sm.next_attempt(self.db, self.task.contract_id),
            error_detail=outcome.error_detail,
        )
        self.db.add(pr)
        self.db.flush()
        self._current_parse_result = pr
        # 转换后 PDF 存 MinIO（前端统一用它渲染）
        if outcome.parse_method.value.startswith("docx_"):
            self._upload_converted_pdf(outcome)
        return pr

    def _upload_converted_pdf(self, outcome) -> None:
        """把 DOCX 转换产物上传到 MinIO，供前端 PDF.js 渲染。

        **只有真实上传成功才登记 `pdf_object_key`**：先 put 再写 key，
        避免出现"库里有 key、MinIO 里没对象"的悬空引用（前端会 404）。

        上传失败只记警告，**不阻断主流程**——审查结果本身不依赖该文件，
        前端拿不到 PDF 时会退化为"仅展示文本"，比整单失败更合理。
        """
        pdf_bytes = getattr(outcome, "pdf_bytes", None)
        if not pdf_bytes:
            # passthrough 降级（无分页）时不会有 PDF 产物
            logger.info("无转换产物可上传（解析方式 %s）", outcome.parse_method.value)
            return

        key = path_converted_pdf(self.task.contract_id)
        try:
            client = get_minio()
            client.put_object(
                settings.minio_bucket_contracts,
                key,
                io.BytesIO(pdf_bytes),
                length=len(pdf_bytes),
                content_type="application/pdf",
            )
        except Exception as exc:  # noqa: BLE001 - 上传失败不阻断审查
            logger.warning("转换后 PDF 上传失败（前端将退化为仅展示文本）: %s", exc)
            return

        contract = self.db.get(Contract, self.task.contract_id)
        if contract is not None:
            contract.pdf_object_key = key
        logger.info(
            "转换后 PDF 已上传: %s/%s（%s 字节）",
            settings.minio_bucket_contracts, key, len(pdf_bytes),
        )

    def _latest_parse_result(self) -> ParseResult:
        pr = getattr(self, "_current_parse_result", None)
        if pr is not None:
            return pr
        pr = self.db.execute(
            select(ParseResult)
            .where(ParseResult.contract_id == self.task.contract_id)
            .order_by(ParseResult.attempt.desc())
            .limit(1)
        ).scalar_one()
        return pr

    def _save_clauses(self, clauses, parse_result_id: int) -> None:
        for c in clauses:
            self.db.add(Clause(
                contract_id=self.task.contract_id,
                parse_result_id=parse_result_id,
                clause_type=c.clause_type.value,
                clause_no=c.clause_no,
                title=c.title,
                content=c.content,
                page_no=c.page_no,
                page_end=c.page_end,
                para_index=c.para_index,
                char_start=c.char_start,
                char_end=c.char_end,
                bbox_x0=c.bbox[0] if c.bbox else None,
                bbox_y0=c.bbox[1] if c.bbox else None,
                bbox_x1=c.bbox[2] if c.bbox else None,
                bbox_y1=c.bbox[3] if c.bbox else None,
                seq=c.seq,
                source=c.source,
            ))

    def _save_metadata(
        self, metadata, parse_result_id: int, builder: AnchorBuilder
    ) -> None:
        """写入元数据，并为其建立锚点。

        锚点用 `owner_type='contract_metadata'`（docs/data-model.md §5.7
        的多态设计），支撑工作台"高亮标记提取的元数据字段"（PRD 2.4.3）。

        **区间来自提取时的正则捕获组**，不是事后重新搜索——同一取值在
        原文出现多次时（如"某某科技有限公司"同时是甲乙方），重新搜索会
        锚到第一处，导致高亮框住错误位置。
        """
        for m in metadata:
            row = ContractMetadata(
                contract_id=self.task.contract_id,
                parse_result_id=parse_result_id,
                meta_key=m.meta_key.value,
                meta_value=m.meta_value,
                value_normalized=m.value_normalized,
                value_type=m.value_type,
                confidence=m.confidence,
                need_review=1 if m.need_review else 0,
            )
            self.db.add(row)
            self.db.flush()  # 需要 row.id 作为 anchor.owner_id

            # 元数据没有可定位区间时不写锚点行（与风险项同一不变量：
            # 无法锚定就不写，绝不伪造位置）
            if m.char_start is None or m.char_end is None:
                continue
            anchor = builder.locate_span(m.char_start, m.char_end)
            if anchor is None or not anchor.anchored:
                logger.info(
                    "元数据 %s 无法锚定原文，跳过写锚点: %r",
                    m.meta_key.value, m.meta_value,
                )
                continue
            self.db.add(Anchor(
                owner_type="contract_metadata",
                owner_id=row.id,
                seq=0,
                page_no=anchor.page_no,
                bbox_x0=anchor.bbox[0],
                bbox_y0=anchor.bbox[1],
                bbox_x1=anchor.bbox[2],
                bbox_y1=anchor.bbox[3],
                char_start=anchor.char_start,
                char_end=anchor.char_end,
                quote_text=anchor.quote_text,
                source=anchor.source.value,
                anchor_level=anchor.level.value,
                confidence=anchor.confidence,
            ))

    def _backfill_contract_fields(self, contract, metadata) -> None:
        """把提取到的元数据回填到 `contract` 的冗余字段。

        只回填**能确定语义**的字段，且**不覆盖已有值**（人工/审批系统填入的优先）：
        - `amount`        ← metadata.amount（归一化后的数字串）
        - `contract_no`   ← metadata.contract_no
        - `counterparty_name` / `counterparty_code` ← 乙方（采购/服务场景下的相对方）

        `applicant` / `applicant_dept` 来自审批系统，不在文档里，不回填。
        """
        if contract is None:
            return
        by_key = {m.meta_key.value: m for m in metadata}

        if contract.amount is None:
            m = by_key.get("amount")
            if m and m.value_normalized:
                try:
                    contract.amount = Decimal(m.value_normalized)
                except (InvalidOperation, ValueError):
                    logger.warning(
                        "任务 %s 金额归一化值无法转 Decimal，跳过回填: %r",
                        self.task.id, m.value_normalized,
                    )
            elif m and m.meta_value:
                # 兼容 value_normalized 缺失的情况
                normalized = _normalize_amount_str(m.meta_value)
                if normalized is not None:
                    contract.amount = normalized

        if not contract.contract_no:
            m = by_key.get("contract_no")
            if m and m.meta_value:
                contract.contract_no = m.meta_value[:64]

        if not contract.counterparty_name:
            m = by_key.get("party_b_name")
            if m and m.meta_value:
                contract.counterparty_name = m.meta_value[:255]

        if not contract.counterparty_code:
            m = by_key.get("party_b_credit_code")
            if m and m.meta_value:
                contract.counterparty_code = m.meta_value[:32]

        if not contract.currency:
            m = by_key.get("currency")
            if m and m.value_normalized:
                contract.currency = m.value_normalized[:8]

    def _save_risks(self, gated, clauses) -> None:
        """落库风险项、锚点与依据链。"""
        for item in gated.items:
            risk = item.risk
            clause_id = None
            if risk.clause_index is not None and 0 <= risk.clause_index < len(clauses):
                # 用 (contract_id, seq) 反查刚写入的 clause 行
                seq = clauses[risk.clause_index].seq
                clause_id = self.db.execute(
                    select(Clause.id).where(
                        Clause.contract_id == self.task.contract_id, Clause.seq == seq
                    )
                ).scalar()

            row = RiskItem(
                contract_id=self.task.contract_id,
                clause_id=clause_id,
                title=risk.title[:255],
                risk_level=risk.risk_level.value,
                category=risk.category,
                reason=risk.reason,
                legal_basis=risk.legal_basis,
                suggestion=risk.suggestion,
                adopted=0,
                merged_by=risk.merged_by.value,
                is_global=1 if risk.is_global else 0,
                unanchored=1 if item.unanchored else 0,
                seq=risk.seq,
            )
            self.db.add(row)
            self.db.flush()

            # 锚点：只有可定位时才写行（不变量：anchor_level=none 不写库）。
            # 一条风险可有多个锚点（跨页条款按页各一个），seq 记录其顺序。
            for seq, a in enumerate(item.anchors):
                if not a.anchored:
                    continue
                self.db.add(Anchor(
                    owner_type="risk_item",
                    owner_id=row.id,
                    seq=seq,
                    page_no=a.page_no,
                    bbox_x0=a.bbox[0],
                    bbox_y0=a.bbox[1],
                    bbox_x1=a.bbox[2],
                    bbox_y1=a.bbox[3],
                    char_start=a.char_start,
                    char_end=a.char_end,
                    quote_text=a.quote_text,
                    source=a.source.value if isinstance(a.source, AnchorSource) else a.source,
                    anchor_level=a.level.value if isinstance(a.level, AnchorLevel) else a.level,
                    confidence=a.confidence,
                ))

            # 依据链
            for ev in risk.evidences:
                self.db.add(RiskEvidence(
                    risk_item_id=row.id,
                    evidence_type=ev.evidence_type,
                    rule_id=ev.rule_id,
                    title=ev.title,
                    detail=ev.detail,
                    raw_snippet=ev.raw_snippet,
                    need_review=1 if ev.need_review else 0,
                ))

    # ==================== 数据加载 ====================

    def _load_rules(self, business_type: str) -> list[Rule]:
        tpl = self.db.execute(
            select(RuleTemplate).where(RuleTemplate.contract_type == business_type)
        ).scalar_one_or_none()
        if tpl is None:
            logger.warning("未找到业务类型 %s 的规则模板，将只跑 LLM 引擎", business_type)
            return []
        rules = list(self.db.execute(
            select(Rule).where(Rule.template_id == tpl.id)
        ).scalars())
        for r in rules:
            _ = list(r.conditions)  # 触发加载，避免后续访问时触发 lazy load
        return rules

    def _load_blacklist_hits(self, metadata) -> dict[str, str]:
        """查主体黑名单。

        ⚠️ 黑名单是 **mock 数据**（docs/data-model.md §5.18），
        不代表真实工商信息。
        """
        names = [
            m.meta_value for m in metadata
            if m.meta_key.value in ("party_a_name", "party_b_name") and m.meta_value
        ]
        if not names:
            return {}
        rows = self.db.execute(
            select(SubjectBlacklist.subject_name, SubjectBlacklist.status)
            .where(SubjectBlacklist.subject_name.in_(names))
        ).fetchall()
        return {name: status for name, status in rows}

    # ==================== 进度 ====================

    def _report_progress(self, parsed: int, total: int) -> None:
        """把进度写 Redis，供前端轮询。

        写失败只记警告：进度是辅助信息，不应因缓存不可用而阻断审查。
        """
        try:
            r = get_redis()
            r.set(key_task_progress(self.task.id),
                  f"{parsed}/{total}", ex=TTL_TASK_PROGRESS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("进度上报失败（不影响审查）: %s", exc)

    def _fail_result(self, detail: str, blocked_reason: str | None = None) -> PipelineResult:
        return PipelineResult(
            task_id=self.task.id,
            status=TaskStatus.BLOCKED.value if blocked_reason else self.task.status,
            blocked_reason=blocked_reason,
            duration_ms=int((time.perf_counter() - self._t0) * 1000),
        )


# ==================== 工具函数 ====================

_META_LABELS = {
    "party_a_name": "甲方名称",
    "party_b_name": "乙方名称",
    "amount": "合同金额",
    "currency": "币种",
}

_META_CATEGORY = {
    "party_a_name": "subject_qualification",
    "party_b_name": "subject_qualification",
    "amount": "amount_payment",
    "currency": "amount_payment",
}


def _normalize_amount_str(raw: str) -> Decimal | None:
    """把形如 "2,299,000.00" 的字符串转为 Decimal；失败返回 None。

    与 `clause_splitter._normalize_amount` 语义一致，此处独立实现以避免
    流水线反向依赖切分模块的私有函数。
    """
    try:
        value = Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    return value.quantize(Decimal("0.01"))


def _meta_label(key: str) -> str:
    return _META_LABELS.get(key, key)


def _meta_category(key: str) -> str:
    return _META_CATEGORY.get(key, "other")


def _conclude(overall: RiskLevel) -> ReviewConclusion:
    """整体风险等级 → 审查结论（docs/data-model.md §3.2）。"""
    if overall is RiskLevel.HIGH:
        return ReviewConclusion.REJECT
    if overall is RiskLevel.MEDIUM:
        return ReviewConclusion.RECTIFY
    return ReviewConclusion.PASS


def _summarize(merged, unanchored_count: int) -> str:
    """生成综合摘要。"""
    counts = merged.counts()
    parts = [
        f"共识别 {len(merged.risks)} 项风险："
        f"高风险 {counts['high']} 项、中风险 {counts['medium']} 项、低风险 {counts['low']} 项。"
    ]
    if merged.both:
        parts.append(f"其中 {merged.both} 项由规则与 AI 双重确认。")
    if unanchored_count:
        parts.append(f"有 {unanchored_count} 项无法定位到原文，需人工核查。")
    if merged.level_upgraded:
        parts.append(f"有 {merged.level_upgraded} 项因 AI 研判更严重而上调等级。")
    return "".join(parts)
