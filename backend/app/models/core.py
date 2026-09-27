"""核心域模型：contract / review_task / parse_result / clause / contract_metadata。

设计依据：docs/data-model.md §5.1 ~ §5.5。
建表顺序见 §12：contract → review_task → parse_result → clause → contract_metadata。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CHAR, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    BlockedReason,
    BusinessType,
    ClauseType,
    ContractSource,
    FileFormat,
    MetadataKey,
    ParseMethod,
    ParseStatus,
    ReviewConclusion,
    RiskLevel,
    TaskStatus,
    WritebackStatus,
    AnchorSource,
)
from app.models.types import (
    BIGINT_UNSIGNED,
    DATETIME_MS,
    INT_UNSIGNED,
    MEDIUM_TEXT,
    bool_col,
    created_at_col,
    deleted_key_col,
    enum_col,
    fk_col,
    money_col,
    pk_col,
    updated_at_col,
)

if TYPE_CHECKING:
    from app.models.risk import RiskItem


class Contract(Base):
    """合同：业务属性与文件引用，是所有其他实体的聚合根。

    软删除仅用于本表（原则 P6）。唯一性靠 `deleted_key` 生成列兜底，
    详见 docs/data-model.md §5.1 的实测说明。
    """

    __tablename__ = "contract"

    id: Mapped[int] = pk_col()
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="合同名称")
    contract_no: Mapped[str | None] = mapped_column(String(64), comment="合同编号")
    business_type: Mapped[str] = enum_col(BusinessType, comment="业务类型，决定规则模板")
    amount: Mapped[Decimal | None] = money_col(comment="合同金额，未提取到为 NULL")
    currency: Mapped[str | None] = mapped_column(
        String(8), default="CNY", server_default=text("'CNY'"), comment="币种"
    )
    applicant: Mapped[str | None] = mapped_column(String(64), comment="申请人")
    applicant_dept: Mapped[str | None] = mapped_column(String(128), comment="送审部门")
    counterparty_name: Mapped[str | None] = mapped_column(String(255), comment="相对方主体名称")
    counterparty_code: Mapped[str | None] = mapped_column(String(32), comment="相对方统一社会信用代码")

    file_format: Mapped[str] = enum_col(FileFormat, comment="文件格式，决定解析路径")
    file_object_key: Mapped[str] = mapped_column(String(512), nullable=False, comment="MinIO 原件 key")
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原始文件名")
    file_size: Mapped[int] = mapped_column(BIGINT_UNSIGNED, nullable=False, comment="字节数")
    file_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, comment="SHA-256，去重用")
    pdf_object_key: Mapped[str | None] = mapped_column(String(512), comment="转换后 PDF 的 key")

    source: Mapped[str] = enum_col(
        ContractSource, default=ContractSource.UPLOAD, comment="来源渠道"
    )
    external_id: Mapped[str | None] = mapped_column(String(128), comment="审批系统单号")

    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME_MS, comment="软删除标记，NULL = 活跃")
    deleted_key: Mapped[datetime] = deleted_key_col()

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    task: Mapped["ReviewTask"] = relationship(
        back_populates="contract",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    parse_results: Mapped[list["ParseResult"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", passive_deletes=True
    )
    clauses: Mapped[list["Clause"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", passive_deletes=True
    )
    metadata_items: Mapped[list["ContractMetadata"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", passive_deletes=True
    )
    risk_items: Mapped[list["RiskItem"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        # 唯一键作用在生成列上：可空列进唯一键时 NULL 之间不冲突，去重会失效
        Index("uk_file_hash", "file_hash", "deleted_key", unique=True),
        Index("idx_business_type", "business_type"),
        Index("idx_created_at", "created_at"),
        Index("idx_external_id", "external_id"),
    )

    def __repr__(self) -> str:
        return f"<Contract id={self.id} title={self.title!r}>"


class ReviewTask(Base):
    """审查任务：承载状态机，是并发控制的锚点（乐观锁 `version`）。"""

    __tablename__ = "review_task"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id", comment="FK → contract.id")

    status: Mapped[str] = enum_col(
        TaskStatus, default=TaskStatus.PENDING, comment="任务状态"
    )
    writeback_status: Mapped[str] = enum_col(
        WritebackStatus, default=WritebackStatus.NOT_WRITTEN, comment="回写状态"
    )
    blocked_reason: Mapped[str | None] = enum_col(
        BlockedReason, nullable=True, comment="仅 blocked 时非空"
    )
    blocked_detail: Mapped[str | None] = mapped_column(String(512), comment="阻塞详情")

    overall_risk: Mapped[str | None] = enum_col(RiskLevel, nullable=True, comment="整体风险等级")
    conclusion: Mapped[str | None] = enum_col(
        ReviewConclusion, nullable=True, comment="审查结论"
    )
    summary: Mapped[str | None] = mapped_column(Text, comment="综合风险摘要")

    total_pages: Mapped[int | None] = mapped_column(comment="解析后确定的总页数")
    parsed_pages: Mapped[int] = mapped_column(default=0, server_default=text("0"), comment="已解析页数")

    # 冗余计数：为列表页服务，避免 N+1 聚合（原则 P4），一致性由 C3 检查兜底
    high_risk_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    medium_risk_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    low_risk_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    parse_duration_ms: Mapped[int | None] = mapped_column(INT_UNSIGNED)
    review_duration_ms: Mapped[int | None] = mapped_column(INT_UNSIGNED)

    started_at: Mapped[datetime | None] = mapped_column(DATETIME_MS, comment="进入 parsing 的时刻")
    blocked_at: Mapped[datetime | None] = mapped_column(DATETIME_MS, comment="进入 blocked 的时刻")
    completed_at: Mapped[datetime | None] = mapped_column(DATETIME_MS, comment="进入 completed 的时刻")

    version: Mapped[int] = mapped_column(
        INT_UNSIGNED, nullable=False, default=0, server_default=text("0"), comment="乐观锁"
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    contract: Mapped["Contract"] = relationship(back_populates="task")
    parse_results: Mapped[list["ParseResult"]] = relationship(back_populates="task")
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("uk_contract_id", "contract_id", unique=True),  # 1:1 约束
        Index("idx_status", "status"),
        Index("idx_status_created", "status", "created_at"),
        Index("idx_overall_risk", "overall_risk"),
    )


class ParseResult(Base):
    """解析结果：一次解析的产物元信息。保留历史以支持重解析对比（D9）。"""

    __tablename__ = "parse_result"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")
    task_id: Mapped[int] = fk_col("review_task.id")

    parse_method: Mapped[str] = enum_col(ParseMethod, comment="实际生效的解析路径")
    converter_used: Mapped[str | None] = mapped_column(String(32), comment="生效的 DOCX 转换器")
    converter_fallback_chain: Mapped[str | None] = mapped_column(
        String(255), comment="降级链，如 wps_com>libreoffice>passthrough"
    )
    ocr_engine: Mapped[str | None] = mapped_column(String(32), comment="OCR 引擎名")
    ocr_dpi: Mapped[int | None] = mapped_column(comment="渲染 DPI")

    page_count: Mapped[int] = mapped_column(nullable=False, comment="总页数")
    failed_pages: Mapped[str | None] = mapped_column(String(512), comment="失败页号，逗号分隔")
    has_text_layer: Mapped[int] = bool_col(comment="是否有原生文本层")
    status: Mapped[str] = enum_col(ParseStatus, comment="解析成败状态")
    avg_confidence: Mapped[float | None] = mapped_column(comment="OCR 平均置信度")

    duration_ms: Mapped[int] = mapped_column(INT_UNSIGNED, nullable=False)
    attempt: Mapped[int] = mapped_column(
        INT_UNSIGNED, nullable=False, default=1, server_default=text("1"), comment="第几次尝试"
    )
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()

    contract: Mapped["Contract"] = relationship(back_populates="parse_results")
    task: Mapped["ReviewTask"] = relationship(back_populates="parse_results")
    clauses: Mapped[list["Clause"]] = relationship(
        back_populates="parse_result", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("idx_contract_attempt", "contract_id", "attempt"),
        Index("idx_task_id", "task_id"),
    )


class Clause(Base):
    """条款：正文的结构化切分结果，**自带位置**（是文档结构，非分析引用）。

    全文不单独存，按 `seq` 拼接 `content` 即可重建（D3，见 §7.2）。
    """

    __tablename__ = "clause"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")
    parse_result_id: Mapped[int] = fk_col("parse_result.id", comment="标明产物来源")

    clause_type: Mapped[str] = enum_col(ClauseType, comment="条款类型")
    clause_no: Mapped[str | None] = mapped_column(String(32), comment="条款序号，如 第三条 / 3.2")
    title: Mapped[str | None] = mapped_column(String(255), comment="条款标题")
    content: Mapped[str] = mapped_column(MEDIUM_TEXT, nullable=False, comment="条款正文")

    page_no: Mapped[int] = mapped_column(nullable=False, comment="起始页码，从 1 开始")
    page_end: Mapped[int | None] = mapped_column(comment="结束页码（跨页条款）")
    para_index: Mapped[int] = mapped_column(nullable=False, comment="段落索引，从 0 开始")

    char_start: Mapped[int | None] = mapped_column(INT_UNSIGNED, comment="全文中的字符起始；扫描件为 NULL")
    char_end: Mapped[int | None] = mapped_column(INT_UNSIGNED, comment="字符结束（不含）")

    bbox_x0: Mapped[float | None] = mapped_column(comment="起始页坐标（PDF point）")
    bbox_y0: Mapped[float | None] = mapped_column()
    bbox_x1: Mapped[float | None] = mapped_column()
    bbox_y1: Mapped[float | None] = mapped_column()

    seq: Mapped[int] = mapped_column(nullable=False, comment="在文档中的顺序")
    source: Mapped[str] = enum_col(AnchorSource, comment="坐标来源")
    created_at: Mapped[datetime] = created_at_col()

    contract: Mapped["Contract"] = relationship(back_populates="clauses")
    parse_result: Mapped["ParseResult"] = relationship(back_populates="clauses")
    risk_items: Mapped[list["RiskItem"]] = relationship(back_populates="clause")

    __table_args__ = (
        Index("idx_contract_seq", "contract_id", "seq"),
        Index("idx_contract_type", "contract_id", "clause_type"),
        Index("idx_parse_result", "parse_result_id"),
    )


class ContractMetadata(Base):
    """合同元数据：从合同提取的键值对，是报告"合同基本信息"的数据源。"""

    __tablename__ = "contract_metadata"

    id: Mapped[int] = pk_col()
    contract_id: Mapped[int] = fk_col("contract.id")
    parse_result_id: Mapped[int] = fk_col("parse_result.id")

    meta_key: Mapped[str] = enum_col(MetadataKey, length=64, comment="元数据键")
    meta_value: Mapped[str | None] = mapped_column(String(1024), comment="原始值（字符串形式）")
    value_normalized: Mapped[str | None] = mapped_column(
        String(512), comment="归一化值（金额→数字串、日期→ISO）"
    )
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="string / decimal / date")
    confidence: Mapped[float] = mapped_column(
        nullable=False, default=1.0, server_default=text("1.0"), comment="提取置信度"
    )
    need_review: Mapped[int] = bool_col(comment="OCR 低置信时置 1，UI 高亮待核对")
    created_at: Mapped[datetime] = created_at_col()

    contract: Mapped["Contract"] = relationship(back_populates="metadata_items")

    __table_args__ = (
        Index("uk_contract_key", "contract_id", "meta_key", unique=True),
        Index("idx_need_review", "contract_id", "need_review"),
    )
