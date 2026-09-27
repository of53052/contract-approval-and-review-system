"""SQLAlchemy ORM 模型汇总。

**导入本模块即注册全部 18 张表**，Alembic 的 autogenerate 依赖这一点
（`env.py` 中 `import app.models`）。

模型按域拆为 4 个文件，避免单文件过长：

| 文件 | 表 | 文档章节 |
|---|---|---|
| `core.py` | contract / review_task / parse_result / clause / contract_metadata | §5.1~§5.5 |
| `risk.py` | risk_item / anchor / risk_evidence | §5.6~§5.8 |
| `collab.py` | task_event / annotation / writeback_log / export_record / llm_call_log | §5.9~§5.13 |
| `config_models.py` | rule_template / rule / rule_condition / standard_clause / subject_blacklist | §5.14~§5.18 |

`enums.py` 定义全部领域枚举；`types.py` 定义列类型与列工厂。
"""

from app.models.collab import (
    Annotation,
    ExportRecord,
    LlmCallLog,
    TaskEvent,
    WritebackLog,
)
from app.models.config_models import (
    Rule,
    RuleCondition,
    RuleTemplate,
    StandardClause,
    SubjectBlacklist,
)
from app.models.core import (
    Clause,
    Contract,
    ContractMetadata,
    ParseResult,
    ReviewTask,
)
from app.models.risk import Anchor, RiskEvidence, RiskItem

__all__ = [
    # 核心域
    "Contract",
    "ReviewTask",
    "ParseResult",
    "Clause",
    "ContractMetadata",
    # 风险域
    "RiskItem",
    "Anchor",
    "RiskEvidence",
    # 协同与审计域
    "TaskEvent",
    "Annotation",
    "WritebackLog",
    "ExportRecord",
    "LlmCallLog",
    # 配置域
    "RuleTemplate",
    "Rule",
    "RuleCondition",
    "StandardClause",
    "SubjectBlacklist",
]

#: 建表顺序（拓扑序，见 docs/data-model.md §12）。
#: `Base.metadata.sorted_tables` 会按外键依赖自动排序，此列表仅用于文档与断言。
TABLE_ORDER: tuple[str, ...] = (
    "contract",
    "review_task",
    "parse_result",
    "clause",
    "contract_metadata",
    "risk_item",
    "anchor",
    "risk_evidence",
    "task_event",
    "annotation",
    "writeback_log",
    "export_record",
    "llm_call_log",
    "rule_template",
    "rule",
    "rule_condition",
    "standard_clause",
    "subject_blacklist",
)
