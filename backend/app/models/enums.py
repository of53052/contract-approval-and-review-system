"""领域枚举字典。

设计依据：docs/data-model.md §3 + 附录 A。

约定（P2）：
- 全部继承 `StrEnum`，序列化即为数据库中的字符串，**不用 MySQL ENUM 类型**，
  新增枚举值无需 DDL 迁移。
- 枚举值一律小写下划线；数据库列宽按最长值预留（见各模型定义）。
- 本模块只放"值集合"，不放业务规则。
"""

from __future__ import annotations

from enum import StrEnum


# ==================== 任务与流程 ====================

class TaskStatus(StrEnum):
    """审查任务状态。状态机见 docs/architecture.md §7.1。"""

    PENDING = "pending"        # 待处理
    PARSING = "parsing"        # 解析中
    REVIEWING = "reviewing"    # 审查中
    BLOCKED = "blocked"        # 解析受阻（终态之一，可重试）
    COMPLETED = "completed"    # 审查完成（终态）


class WritebackStatus(StrEnum):
    """回写审批系统的状态。"""

    NOT_WRITTEN = "not_written"
    WRITING = "writing"
    SUCCESS = "success"
    FAILED = "failed"


class BlockedReason(StrEnum):
    """解析受阻原因。"""

    ENCRYPTED = "encrypted"                          # 文档已加密
    EMPTY_CONTENT = "empty_content"                  # 正文为空
    BLURRED = "blurred"                              # 扫描件严重模糊
    TIMEOUT = "timeout"                              # 处理超时
    CONVERTER_UNAVAILABLE = "converter_unavailable"  # 转换器不可用
    ERROR = "error"                                  # 其他异常兜底


class TaskEventType(StrEnum):
    """任务事件类型（审计轨迹）。"""

    STATUS_CHANGE = "status_change"
    RETRY = "retry"
    BLOCKED = "blocked"
    PROGRESS = "progress"


# ==================== 风险 ====================

class RiskLevel(StrEnum):
    """风险等级。合并策略为"取高不取低"，见 docs/architecture.md §8.3。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskCategory(StrEnum):
    """风险分类，与 `ClauseType` 部分重叠但语义不同（前者是风险维度，后者是条款维度）。"""

    SUBJECT_QUALIFICATION = "subject_qualification"
    AMOUNT_PAYMENT = "amount_payment"
    LIABILITY = "liability"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    JURISDICTION = "jurisdiction"
    CONFIDENTIALITY = "confidentiality"
    DATA_SECURITY = "data_security"
    ACCEPTANCE = "acceptance"
    FORCE_MAJEURE = "force_majeure"
    OTHER = "other"


class MergedBy(StrEnum):
    """风险项的定级来源，配合 `risk_evidence` 实现双来源留痕。"""

    RULE = "rule"      # 仅规则命中
    LLM = "llm"        # 仅 LLM 研判
    BOTH = "both"      # 两者均命中
    MANUAL = "manual"  # 人工添加


class ReviewConclusion(StrEnum):
    """审查结论。"""

    PASS = "pass"        # 通过
    RECTIFY = "rectify"  # 建议整改
    REJECT = "reject"    # 建议拒绝


class EvidenceType(StrEnum):
    """风险依据来源。"""

    RULE = "rule"
    LLM = "llm"
    LAW = "law"
    MANUAL = "manual"


# ==================== 条款与解析 ====================

class ClauseType(StrEnum):
    """条款类型。"""

    SUBJECT_MATTER = "subject_matter"
    PAYMENT = "payment"
    ACCEPTANCE = "acceptance"
    LIABILITY = "liability"
    CONFIDENTIALITY = "confidentiality"
    DATA_SECURITY = "data_security"
    JURISDICTION = "jurisdiction"
    INTELLECTUAL_PROPERTY = "intellectual_property"
    FORCE_MAJEURE = "force_majeure"
    OTHER = "other"


class ParseMethod(StrEnum):
    """实际生效的解析路径。"""

    DOCX_WPS_COM = "docx_wps_com"            # DOCX 经 WPS 转 PDF
    DOCX_LIBREOFFICE = "docx_libreoffice"    # DOCX 经 LibreOffice 转 PDF
    DOCX_PASSTHROUGH = "docx_passthrough"    # DOCX 无分页降级
    PDF_NATIVE = "pdf_native"                # 文本型 PDF 直接提取
    OCR_RAPIDOCR = "ocr_rapidocr"            # 扫描件 OCR


class ParseStatus(StrEnum):
    """单次解析的成败状态。"""

    SUCCESS = "success"
    PARTIAL = "partial"  # 部分页失败
    FAILED = "failed"


class AnchorSource(StrEnum):
    """锚点坐标的来源，决定前端高亮的可信度提示。"""

    NATIVE_TEXT = "native_text"
    OCR = "ocr"


class AnchorLevel(StrEnum):
    """引用对齐的降级层级，见 docs/architecture.md §6.4。

    注意：`NONE` 仅用于内存中的中间结果，**不写库**；
    无法锚定的风险项改用 `risk_item.unanchored = 1` 表达。
    """

    EXACT = "exact"
    FUZZY = "fuzzy"
    PARAGRAPH = "paragraph"
    NONE = "none"


class FileFormat(StrEnum):
    """合同文件格式，决定解析路径分派。"""

    DOCX = "docx"
    PDF = "pdf"
    SCANNED_PDF = "scanned_pdf"
    IMAGE = "image"


class ContractSource(StrEnum):
    """合同来源渠道。"""

    UPLOAD = "upload"
    APPROVAL_SYNC = "approval_sync"


# ==================== 配置 ====================

class BusinessType(StrEnum):
    """合同业务类型，决定命中哪套规则模板。"""

    PURCHASE = "purchase"
    SALES = "sales"
    SERVICE = "service"
    LABOR = "labor"


class RuleType(StrEnum):
    """规则匹配方式。"""

    KEYWORD = "keyword"      # 关键词匹配
    REGEX = "regex"          # 正则匹配
    THRESHOLD = "threshold"  # 阈值比较
    PRESENCE = "presence"    # 存在性检查（必备条款）
    BLACKLIST = "blacklist"  # 黑名单匹配


class RuleOperator(StrEnum):
    """规则条件运算符。"""

    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    REGEX = "regex"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    EXISTS = "exists"
    NOT_EXISTS = "not_exists"


class ValueType(StrEnum):
    """规则条件值 / 元数据值的类型标签。"""

    STRING = "string"
    DECIMAL = "decimal"
    DATE = "date"
    REGEX = "regex"
    LIST = "list"


class MetadataKey(StrEnum):
    """合同元数据键。必需性与缺失后果见 docs/data-model.md §3.5。"""

    PARTY_A_NAME = "party_a_name"
    PARTY_B_NAME = "party_b_name"
    PARTY_A_CREDIT_CODE = "party_a_credit_code"
    PARTY_B_CREDIT_CODE = "party_b_credit_code"
    CONTRACT_NO = "contract_no"
    AMOUNT = "amount"
    CURRENCY = "currency"
    TERM = "term"
    EFFECTIVE_CONDITION = "effective_condition"
    SIGN_DATE = "sign_date"
    SIGN_PLACE = "sign_place"


# ==================== 便捷集合（供不变量校验与一致性脚本复用）====================

#: 无原生文本层的解析方式：这些路径产出的锚点 source 必须是 OCR
OCR_PARSE_METHODS: frozenset[ParseMethod] = frozenset({ParseMethod.OCR_RAPIDOCR})

#: 有原生文本层的解析方式
TEXT_LAYER_PARSE_METHODS: frozenset[ParseMethod] = frozenset(
    {
        ParseMethod.PDF_NATIVE,
        ParseMethod.DOCX_WPS_COM,
        ParseMethod.DOCX_LIBREOFFICE,
    }
)

#: 枚举列的显式宽度。
#:
#: ⚠️ **不要用"当前枚举最长值"当列宽**。那样每加一个更长的枚举值都要改 DDL，
#: 直接违背原则 P2（新增枚举值不需要 DDL）。这里按 docs/data-model.md §5 的
#: 规定宽度固定下来，留出扩展余量。
ENUM_WIDTH: dict[str, int] = {
    # 任务与流程
    "TaskStatus": 16,
    "WritebackStatus": 16,
    "BlockedReason": 32,
    "TaskEventType": 32,
    # 风险
    "RiskLevel": 8,
    "RiskCategory": 32,
    "MergedBy": 8,
    "ReviewConclusion": 16,
    "EvidenceType": 16,
    # 条款与解析
    "ClauseType": 32,
    "ParseMethod": 32,
    "ParseStatus": 16,
    "AnchorSource": 16,
    "AnchorLevel": 16,
    "FileFormat": 16,
    "ContractSource": 16,
    # 配置
    "BusinessType": 32,
    "RuleType": 16,
    "RuleOperator": 16,
    "ValueType": 16,
    "MetadataKey": 64,
}

#: 风险等级排序权重，用于"取高不取低"合并
RISK_LEVEL_WEIGHT: dict[RiskLevel, int] = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}
