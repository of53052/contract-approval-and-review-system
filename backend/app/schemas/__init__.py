"""Pydantic 响应模型。

**只放对外契约**：请求/响应结构在这里定义，ORM 模型不直接暴露给前端。
这样数据库字段改名不会无声地破坏前端契约。
"""

from app.schemas.common import ErrorOut, OkOut, Page
from app.schemas.contracts import (
    BatchIn,
    BatchItemResult,
    BatchResultOut,
    ClauseOut,
    ContractDetail,
    ContractListItem,
    MetadataOut,
    TaskEventOut,
)
from app.schemas.risks import (
    AnchorOut,
    AnnotationIn,
    AnnotationOut,
    EvidenceOut,
    RiskItemOut,
    RiskUpdateIn,
)
from app.schemas.rules import (
    BlacklistOut,
    RuleConditionIn,
    RuleConditionOut,
    RuleIn,
    RuleOptionsOut,
    RuleOut,
    RuleTemplateOut,
    RuleTemplateUpdateIn,
    RuleUpdateIn,
    StandardClauseIn,
    StandardClauseOut,
    StandardClauseUpdateIn,
)
from app.schemas.tasks import (
    ExportOut,
    ReportPreviewOut,
    TaskOut,
    TaskProgressOut,
    UploadResultOut,
    WritebackIn,
    WritebackOut,
    WritebackStatusOut,
)

__all__ = [
    "ErrorOut", "OkOut", "Page",
    "BatchIn", "BatchItemResult", "BatchResultOut",
    "ClauseOut", "ContractDetail", "ContractListItem", "MetadataOut", "TaskEventOut",
    "AnchorOut", "AnnotationIn", "AnnotationOut", "EvidenceOut", "RiskItemOut",
    "RiskUpdateIn",
    "BlacklistOut", "RuleConditionOut", "RuleOut", "RuleTemplateOut", "StandardClauseOut",
    "RuleConditionIn", "RuleIn", "RuleUpdateIn", "RuleTemplateUpdateIn",
    "StandardClauseIn", "StandardClauseUpdateIn", "RuleOptionsOut",
    "ExportOut", "ReportPreviewOut", "TaskOut", "TaskProgressOut", "UploadResultOut",
    "WritebackIn", "WritebackOut", "WritebackStatusOut",
]
