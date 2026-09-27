"""种子数据：规则模板 / 规则 / 条件 / 标准条款 / 黑名单。

设计依据：docs/data-model.md §13。

⚠️ **阈值是演示用拟值，不是法律意见**（架构文档 §8.5 原文）。
   实际使用前必须由法务校准，见 docs/data-model.md 附录 B 第 5 项。

幂等：全部按业务键 upsert，可重复执行。
用法：
    python scripts/seed_data.py            # 写入（幂等）
    python scripts/seed_data.py --dry-run  # 只打印将写入的内容
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Rule,
    RuleCondition,
    RuleTemplate,
    StandardClause,
    SubjectBlacklist,
)
from app.models.enums import (  # noqa: E402
    BusinessType,
    ClauseType,
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
    ValueType,
)


# ==================== 规则模板 ====================

TEMPLATES: list[dict] = [
    {"contract_type": BusinessType.PURCHASE, "name": "采购合同审查清单",
     "description": "适用于我方作为采购方的合同"},
    {"contract_type": BusinessType.SALES, "name": "销售合同审查清单",
     "description": "适用于我方作为销售方的合同"},
    {"contract_type": BusinessType.SERVICE, "name": "服务合同审查清单",
     "description": "适用于技术服务/外包合同"},
    {"contract_type": BusinessType.LABOR, "name": "劳动合同审查清单",
     "description": "适用于劳动用工合同"},
]


# ==================== 规则（对应架构文档 §8.5 阈值表）====================
# 字段：code / name / category / risk_level / rule_type / config /
#       result_template / suggestion_template / conditions
#
# ⚠️ **按业务类型分组**（批次 9 修正）：原先所有规则都挂在"采购合同"模板下，
# 其余三个模板是空壳。那时只有采购合同是真实输入，问题不显；一旦有销售/劳动合同，
# "挂错模板"就变成两类实际故障：
#   ① 销售/劳动合同加载不到任何规则 → 确定性规则引擎 0 命中，只剩 LLM；
#   ② 采购规则（要求"标的物""付款条款内必须提验收"）与劳动场景完全不搭。
# 每类合同只挂它真正适用的规则，模板树的数字才有意义。

def _c(field: str, operator: RuleOperator, value: str | None,
       value_type: ValueType = ValueType.STRING) -> dict:
    """规则条件的简写构造器。"""
    return {"field": field, "operator": operator, "value": value, "value_type": value_type}


# ---------- 采购合同（我方为采购方）----------
_PURCHASE_RULES: list[dict] = [
    # ---- 高风险 ----
    {
        "code": "LIABILITY_UNCAPPED",
        "name": "违约责任无上限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        "config": {"keywords": ["无上限", "不设上限", "不受限制", "全部损失", "一切损失"],
                   "match_all": False},
        "result_template": "违约责任未设赔偿上限，我方责任敞口不可预估。",
        "suggestion_template": "建议增加责任上限条款：任一方承担的赔偿责任总额不超过合同总金额。",
        "conditions": [_c("clause.content", RuleOperator.CONTAINS, "无上限")],
    },
    {
        "code": "LIABILITY_ASYMMETRY",
        "name": "违约责任不对等",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.THRESHOLD,
        # PRD 2.4.9 场景二要求识别"客户方万分之一滞纳金、我方无上限赔偿"这类
        # 责任严重不对等的条款。它不是"某个比例超限"，而是**两种表述并存**，
        # 因此用 liability_asymmetry（详见 rule_engine._check_liability_asymmetry）。
        # ⚠️ 原先这个位置是一条 penalty_ratio > 0.2 的规则，与中风险的
        # PENALTY_OVER_LIMIT 配置完全相同——同一事实必报两条（一条 high 一条
        # medium），既重复又掩盖了"不对等"这个真正要判的语义。
        "config": {
            "metric": "liability_asymmetry",
            "severe_keywords": ["全部损失", "一切损失", "无上限", "不设上限", "不受限制"],
            "mild_keywords": ["万分之一", "万分之五", "累计不超过", "不超过应付未付金额"],
            "mild_ratio_max": 0.05,
        },
        "result_template": "双方违约责任明显不对等：一方承担无上限赔偿责任，另一方仅承担极小比例责任。",
        "suggestion_template": "建议对等约定：双方均按合同总金额的同一比例承担违约责任，"
                               "并均设置不超过合同总金额的赔偿上限。",
        "conditions": [],
    },
    {
        "code": "JURISDICTION_INVALID",
        "name": "管辖地约定违规",
        "category": RiskCategory.JURISDICTION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"blacklist": ["境外仲裁", "香港仲裁", "新加坡仲裁", "对方所在地法院",
                                 "供应商所在地法院", "乙方所在地法院"]},
        "result_template": "争议管辖地约定不利于我方，可能显著提高维权成本。",
        "suggestion_template": "建议改为由我方所在地有管辖权的人民法院管辖。",
        # BLACKLIST 会读 conditions 里的 regex（见 rule_engine._apply_blacklist）
        "conditions": [_c("clause.content", RuleOperator.REGEX,
                          "境外仲裁|香港仲裁|新加坡仲裁|对方所在地法院|供应商所在地法院|乙方所在地法院")],
    },
    {
        "code": "NO_ACCEPTANCE_BEFORE_PAY",
        "name": "未设置付款前置验收",
        "category": RiskCategory.ACCEPTANCE,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        # 语义是"付款条款内必须提到验收"，而非"合同要有验收条款"。
        # 两者不等价：实测样本同时有「验收标准」与「到货即付全款」，
        # 用后者判定会漏报（见 rule_engine._apply_presence 的说明）。
        "config": {
            "within_clause_type": ClauseType.PAYMENT.value,
            "required_pattern": "验收",
            # 锚点定位提示（批次 9）：本条风险说的是**付款条款**有问题，
            # 但样本里「合同金额」与「付款方式」同属 payment 类型，
            # 不指定就会锚到前者。引擎优先在"缺该模式的那类条款"里挑
            # 含这些词的条目（见 rule_engine._guess_anchor_clause）。
            # 锚错不仅高亮指向错误段落，还会让它与 LLM 的同类结论
            # （引文落在付款条款）无法合并，同一事实出现两张卡片。
            "anchor_keywords": ["付款", "支付"],
        },
        "result_template": "付款条款未以验收为前置条件，存在先付款后验收风险。",
        "suggestion_template": "建议约定：甲方验收合格并出具验收单后，方支付相应款项。",
        # ⚠️ 这里**故意不写 conditions**：规则引擎从不读 `clause.payment.content`
        # 这类字段（`clause.` 后面只能是 content / clause_type / clause_no / title）。
        # "付款条款内必须提到验收"由 config.within_clause_type + required_pattern 表达。
        "conditions": [],
    },
    {
        "code": "IP_TRANSFER_ALL",
        "name": "知识产权全部转让对方",
        "category": RiskCategory.INTELLECTUAL_PROPERTY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        # 关键词需覆盖真实合同里的多种表述（"归供应商所有""所有权归乙方"等）。
        # 实测教训：只写"知识产权归对方所有"会漏掉"归供应商所有"这类同义写法。
        "config": {"keywords": [
            "知识产权归供应商所有", "知识产权归乙方所有", "知识产权归对方所有",
            "知识产权归甲方所有", "全部知识产权归", "知识产权均归",
            "所有权归供应商", "所有权归乙方", "成果归供应商",
        ], "match_all": False},
        "result_template": "知识产权归属约定不利于我方，可能丧失核心成果所有权。",
        "suggestion_template": "建议约定：本项目产生的知识产权归我方所有，对方仅享有使用权。",
        # KEYWORD 只读 contains 条件，正则不会被读取（原先挂在这里的正则条件
        # 因此是死配置），同义写法统一由 config.keywords 覆盖。
        "conditions": [],
    },
    {
        "code": "SUBJECT_MISSING",
        "name": "主体信息缺失",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["party_a_name", "party_b_name"]},
        "result_template": "合同主体信息不完整，无法核实对方资质。",
        "suggestion_template": "建议补全双方全称与统一社会信用代码。",
        "conditions": [
            _c("metadata.party_a_name", RuleOperator.EXISTS, None),
            _c("metadata.party_b_name", RuleOperator.EXISTS, None),
        ],
    },
    {
        "code": "SUBJECT_ABNORMAL",
        "name": "主体列入经营异常",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"source": "subject_blacklist", "match_field": "subject_name"},
        "result_template": "相对方主体被列入经营异常名录，履约能力存疑。",
        "suggestion_template": "建议要求对方提供资质证明，或提供履约担保后再签约。",
        "conditions": [_c("metadata.party_b_name", RuleOperator.EXISTS, None)],
    },
    # ---- 中风险 ----
    {
        "code": "PENALTY_OVER_LIMIT",
        "name": "违约金比例超限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.THRESHOLD,
        "config": {"threshold": 0.20, "metric": "penalty_ratio", "direction": "gt"},
        "result_template": "违约金比例超过 {threshold} 的参考上限。",
        "suggestion_template": "建议将违约金比例下调至合同总金额的 20% 以内。",
        "conditions": [_c("metadata.amount", RuleOperator.EXISTS, None)],
    },
    {
        "code": "CONFIDENTIALITY_NO_TERM",
        "name": "保密义务无期限",
        "category": RiskCategory.CONFIDENTIALITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "保密期限"},
        "result_template": "保密条款未明确期限，义务边界不清晰。",
        "suggestion_template": "建议约定：保密义务自签署之日起持续 {n} 年。",
        # 原先挂着 `clause.content not_contains 保密期限`。引擎在任何类型下都不读
        # not_contains（presence 只读 not_exists），是死配置；语义已由
        # config.required_pattern 完整表达（批次 9 由写入校验统一挡掉）。
        "conditions": [],
    },
    {
        "code": "FORCE_MAJEURE_NO_NOTICE",
        "name": "不可抗力无通知时效",
        "category": RiskCategory.FORCE_MAJEURE,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "日内通知"},
        "result_template": "不可抗力条款未约定通知时效，事后举证易生争议。",
        "suggestion_template": "建议约定：受影响方应在不可抗力发生后 15 日内书面通知对方。",
        "conditions": [],  # 同 CONFIDENTIALITY_NO_TERM，原 not_contains 条件不生效
    },
    {
        "code": "AMOUNT_MISSING",
        "name": "合同金额缺失",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["amount"]},
        "result_template": "未提取到合同金额，影响后续比例类规则判定。",
        "suggestion_template": "建议在合同中以大写与小写并列方式明确合同总金额。",
        "conditions": [_c("metadata.amount", RuleOperator.EXISTS, None)],
    },
    {
        "code": "CURRENCY_MISSING",
        "name": "币种缺失",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["currency"]},
        "result_template": "未明确币种，涉外场景下金额存在歧义。",
        "suggestion_template": "建议明确约定合同币种（如人民币 CNY）。",
        "conditions": [_c("metadata.currency", RuleOperator.EXISTS, None)],
    },
]


# ---------- 销售合同（我方为供方）----------
# 与采购的差别在**立场**：我方交付、对方付款。因此"付款前置验收"这类
# 采购专属要求不适用（我们不能用拒付来制衡客户），而"责任不对等"是
# PRD 2.4.9 场景二点名的核心风险。
_SALES_RULES: list[dict] = [
    {
        "code": "LIABILITY_ASYMMETRY",
        "name": "违约责任不对等",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.THRESHOLD,
        "config": {
            "metric": "liability_asymmetry",
            "severe_keywords": ["全部损失", "一切损失", "无上限", "不设上限", "不受限制"],
            "mild_keywords": ["万分之一", "万分之五", "累计不超过", "不超过应付未付金额"],
            "mild_ratio_max": 0.05,
        },
        "result_template": "双方违约责任明显不对等：我方承担无上限赔偿责任，"
                           "而对方仅承担极小比例责任。",
        "suggestion_template": "建议对等约定：双方均按合同总金额的同一比例承担违约责任，"
                               "并均设置不超过合同总金额的赔偿上限。",
        "conditions": [],
    },
    {
        "code": "LIABILITY_UNCAPPED",
        "name": "违约责任无上限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        "config": {"keywords": ["无上限", "不设上限", "不受限制", "全部损失", "一切损失"],
                   "match_all": False},
        "result_template": "违约责任未设赔偿上限，我方责任敞口不可预估。",
        "suggestion_template": "建议增加责任上限条款：任一方承担的赔偿责任总额不超过合同总金额。",
        "conditions": [],
    },
    {
        "code": "JURISDICTION_INVALID",
        "name": "管辖地约定违规",
        "category": RiskCategory.JURISDICTION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"blacklist": ["境外仲裁", "香港仲裁", "新加坡仲裁", "对方所在地法院",
                                 "客户所在地法院", "乙方所在地法院"]},
        "result_template": "争议管辖地约定不利于我方，可能显著提高维权成本。",
        "suggestion_template": "建议改为由我方所在地有管辖权的人民法院管辖。",
        "conditions": [],
    },
    {
        "code": "SUBJECT_MISSING",
        "name": "主体信息缺失",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["party_a_name", "party_b_name"]},
        "result_template": "合同主体信息不完整，无法核实对方资质。",
        "suggestion_template": "建议补全双方全称与统一社会信用代码。",
        "conditions": [],
    },
    {
        "code": "SUBJECT_ABNORMAL",
        "name": "主体列入经营异常",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"source": "subject_blacklist", "match_field": "subject_name"},
        "result_template": "相对方主体被列入经营异常名录，履约能力存疑。",
        "suggestion_template": "建议要求对方提供资质证明，或提供履约担保后再签约。",
        "conditions": [],
    },
    {
        "code": "PENALTY_OVER_LIMIT",
        "name": "违约金比例超限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.THRESHOLD,
        "config": {"threshold": 0.20, "metric": "penalty_ratio", "direction": "gt"},
        "result_template": "违约金比例超过 {threshold} 的参考上限。",
        "suggestion_template": "建议将违约金比例下调至合同总金额的 20% 以内。",
        "conditions": [],
    },
    {
        "code": "CONFIDENTIALITY_NO_TERM",
        "name": "保密义务无期限",
        "category": RiskCategory.CONFIDENTIALITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "保密期限"},
        "result_template": "保密条款未明确期限，义务边界不清晰。",
        "suggestion_template": "建议约定：保密义务自签署之日起持续 {n} 年。",
        "conditions": [],
    },
    {
        "code": "FORCE_MAJEURE_NO_NOTICE",
        "name": "不可抗力无通知时效",
        "category": RiskCategory.FORCE_MAJEURE,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "日内通知"},
        "result_template": "不可抗力条款未约定通知时效，事后举证易生争议。",
        "suggestion_template": "建议约定：受影响方应在不可抗力发生后 15 日内书面通知对方。",
        "conditions": [],
    },
]


# ---------- 劳动合同（用人单位视角）----------
# ⚠️ 注意：劳动合同**没有"合同金额/币种"**，因此不挂 AMOUNT_MISSING /
# CURRENCY_MISSING；`global_checker` 也已按业务类型跳过这两项，否则每份
# 劳动合同都会稳定报一条假风险。
_LABOR_RULES: list[dict] = [
    {
        "code": "PROBATION_PAY_LOW",
        "name": "试用期工资低于法定下限",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.THRESHOLD,
        # 《劳动合同法》第二十条：试用期工资不得低于本单位相同岗位最低档工资
        # 或者劳动合同约定工资的 80%。
        "config": {"metric": "probation_pay_ratio", "threshold": 80, "direction": "lt"},
        "result_template": "试用期工资低于劳动合同约定工资的 80%，违反《劳动合同法》第二十条。",
        "suggestion_template": "建议将试用期工资调整为不低于劳动合同约定工资的 80%，"
                               "且不低于本单位相同岗位最低档工资。",
        "conditions": [],
    },
    {
        "code": "SOCIAL_INSURANCE_WAIVED",
        "name": "约定放弃缴纳社会保险",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        # 社保是法定强制义务，任何"自愿放弃"的约定均无效，且用人单位
        # 仍需承担补缴与工伤赔付责任。
        "config": {"keywords": ["自愿放弃", "放弃缴纳社会保险", "不缴纳社会保险",
                                "社保补贴", "放弃社保"], "match_all": False},
        "result_template": "约定劳动者放弃社会保险属无效条款，用人单位仍须承担补缴与工伤赔偿责任。",
        "suggestion_template": "建议删除该约定，依法为劳动者办理社会保险登记并足额缴纳。",
        "conditions": [],
    },
    {
        "code": "WORKER_LIQUIDATED_DAMAGES",
        "name": "对劳动者约定违约金",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        # 《劳动合同法》第二十五条：除服务期与竞业限制两种情形外，
        # 用人单位不得与劳动者约定由劳动者承担违约金。
        "config": {"keywords": ["提前离职的，应向甲方支付违约金",
                                "应向甲方支付违约金", "支付违约金人民币"],
                   "match_all": False},
        "result_template": "除服务期、竞业限制外约定劳动者承担违约金，违反《劳动合同法》第二十五条。",
        "suggestion_template": "建议删除对劳动者的一般性违约金约定；"
                               "如确需约束，应改为合法的服务期约定并支付相应培训费用对价。",
        "conditions": [],
    },
    {
        "code": "OVERTIME_WAIVED",
        "name": "约定不支付加班费",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        "config": {"keywords": ["不另行支付加班费", "不支付加班费", "每周工作六天",
                                "加班费已包含在工资内"], "match_all": False},
        "result_template": "约定不支付加班费违反《劳动法》第四十四条，且超时工作本身即属违法。",
        "suggestion_template": "建议按法定标准支付加班费，并将工作时间调整为每周不超过 40 小时。",
        "conditions": [],
    },
    {
        "code": "NON_COMPETE_NO_COMPENSATION",
        "name": "竞业限制未约定补偿",
        "category": RiskCategory.CONFIDENTIALITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        # 《劳动合同法》第二十三条：约定竞业限制的，应在解除或终止后
        # 按月给予经济补偿；未约定补偿的，劳动者可主张按法定标准补足。
        "config": {"within_clause_type": ClauseType.CONFIDENTIALITY.value,
                   "required_pattern": "补偿"},
        "result_template": "约定竞业限制但未约定经济补偿，该条款对劳动者的约束力存在争议。",
        "suggestion_template": "建议约定：竞业限制期内按月支付不低于离职前十二个月"
                               "平均工资 30% 的经济补偿。",
        "conditions": [],
    },
    {
        "code": "CONFIDENTIALITY_NO_TERM",
        "name": "保密义务无期限",
        "category": RiskCategory.CONFIDENTIALITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "保密期限"},
        "result_template": "保密条款未明确期限，义务边界不清晰。",
        "suggestion_template": "建议约定：保密义务自签署之日起持续 {n} 年。",
        "conditions": [],
    },
    {
        "code": "SUBJECT_MISSING",
        "name": "主体信息缺失",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["party_a_name", "party_b_name"]},
        "result_template": "合同主体信息不完整，无法核实对方资质。",
        "suggestion_template": "建议补全双方全称与统一社会信用代码。",
        "conditions": [],
    },
]


# ---------- 服务合同（我方为委托方）----------
# 立场与采购相同（我方付款、对方交付），但风险面有自己的侧重：
#   · **成果归属**是服务/外包场景的头号风险——成果是无形的，一旦归对方，
#     我方既失去所有权又难以事后替换（采购有实物可退换，服务没有）；
#   · **付款前置验收**同样成立，且服务验收标准更模糊，不挂钩风险更高；
#   · 复用采购的通用项（责任、管辖、主体、金额、保密、不可抗力）。
_SERVICE_RULES: list[dict] = [
    {
        "code": "LIABILITY_ASYMMETRY",
        "name": "违约责任不对等",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.THRESHOLD,
        # 同采购/销售：判"重责表述与轻责表述并存"，详见
        # rule_engine._check_liability_asymmetry。
        "config": {
            "metric": "liability_asymmetry",
            "severe_keywords": ["全部损失", "一切损失", "无上限", "不设上限", "不受限制"],
            "mild_keywords": ["万分之一", "万分之五", "累计不超过", "不超过应付未付金额"],
            "mild_ratio_max": 0.05,
        },
        "result_template": "双方违约责任明显不对等：一方承担无上限赔偿责任，另一方仅承担极小比例责任。",
        "suggestion_template": "建议对等约定：双方均按合同总金额的同一比例承担违约责任，"
                               "并均设置不超过合同总金额的赔偿上限。",
        "conditions": [],
    },
    {
        "code": "LIABILITY_UNCAPPED",
        "name": "违约责任无上限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        "config": {"keywords": ["无上限", "不设上限", "不受限制", "全部损失", "一切损失"],
                   "match_all": False},
        "result_template": "违约责任未设赔偿上限，我方责任敞口不可预估。",
        "suggestion_template": "建议增加责任上限条款：任一方承担的赔偿责任总额不超过合同总金额。",
        "conditions": [],
    },
    {
        "code": "JURISDICTION_INVALID",
        "name": "管辖地约定违规",
        "category": RiskCategory.JURISDICTION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"blacklist": ["境外仲裁", "香港仲裁", "新加坡仲裁", "对方所在地法院",
                                 "服务方所在地法院", "乙方所在地法院"]},
        "result_template": "争议管辖地约定不利于我方，可能显著提高维权成本。",
        "suggestion_template": "建议改为由我方所在地有管辖权的人民法院管辖。",
        "conditions": [],
    },
    {
        "code": "NO_ACCEPTANCE_BEFORE_PAY",
        "name": "未设置付款前置验收",
        "category": RiskCategory.ACCEPTANCE,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        # 语义是"付款条款内必须提到验收"。服务成果验收标准天然模糊，
        # 不挂钩付款时我方几乎失去履约抗辩手段（同采购，见 _apply_presence）。
        "config": {
            "within_clause_type": ClauseType.PAYMENT.value,
            "required_pattern": "验收",
            # 样本里「合同金额」与「付款方式」同属 payment 类，
            # 不指定会锚到前者（同采购批次 9 的修复）。
            "anchor_keywords": ["付款", "支付"],
        },
        "result_template": "付款条款未以验收为前置条件，存在先付款后验收风险。",
        "suggestion_template": "建议约定：甲方验收合格并出具验收单后，方支付相应款项。",
        "conditions": [],
    },
    {
        "code": "IP_TRANSFER_ALL",
        "name": "知识产权全部转让对方",
        "category": RiskCategory.INTELLECTUAL_PROPERTY,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.KEYWORD,
        # 服务/外包场景的头号风险：成果是无形的，归属一旦给出去，
        # 我方既失去所有权，又难以像采购那样"退货换货"。关键词需覆盖
        # "知识产权归乙方/服务方所有""服务成果归对方"等多种表述。
        "config": {"keywords": [
            "知识产权归供应商所有", "知识产权归乙方所有", "知识产权归对方所有",
            "知识产权归甲方所有", "全部知识产权归", "知识产权均归",
            "所有权归供应商", "所有权归乙方", "成果归供应商", "成果归乙方",
            "服务成果归乙方", "交付物归乙方",
        ], "match_all": False},
        "result_template": "知识产权归属约定不利于我方，可能丧失核心成果所有权。",
        "suggestion_template": "建议约定：本项目专门产生的服务成果与交付物知识产权归我方所有，"
                               "对方既有知识产权仍归其所有并授予我方必要许可。",
        "conditions": [],
    },
    {
        "code": "SUBJECT_MISSING",
        "name": "主体信息缺失",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["party_a_name", "party_b_name"]},
        "result_template": "合同主体信息不完整，无法核实对方资质。",
        "suggestion_template": "建议补全双方全称与统一社会信用代码。",
        "conditions": [
            _c("metadata.party_a_name", RuleOperator.EXISTS, None),
            _c("metadata.party_b_name", RuleOperator.EXISTS, None),
        ],
    },
    {
        "code": "SUBJECT_ABNORMAL",
        "name": "主体列入经营异常",
        "category": RiskCategory.SUBJECT_QUALIFICATION,
        "risk_level": RiskLevel.HIGH,
        "rule_type": RuleType.BLACKLIST,
        "config": {"source": "subject_blacklist", "match_field": "subject_name"},
        "result_template": "相对方主体被列入经营异常名录，履约能力存疑。",
        "suggestion_template": "建议要求对方提供资质证明，或提供履约担保后再签约。",
        "conditions": [_c("metadata.party_b_name", RuleOperator.EXISTS, None)],
    },
    # ---- 中风险 ----
    {
        "code": "PENALTY_OVER_LIMIT",
        "name": "违约金比例超限",
        "category": RiskCategory.LIABILITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.THRESHOLD,
        "config": {"threshold": 0.20, "metric": "penalty_ratio", "direction": "gt"},
        "result_template": "违约金比例超过 {threshold} 的参考上限。",
        "suggestion_template": "建议将违约金比例下调至合同总金额的 20% 以内。",
        "conditions": [_c("metadata.amount", RuleOperator.EXISTS, None)],
    },
    {
        "code": "CONFIDENTIALITY_NO_TERM",
        "name": "保密义务无期限",
        "category": RiskCategory.CONFIDENTIALITY,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "保密期限"},
        "result_template": "保密条款未明确期限，义务边界不清晰。",
        "suggestion_template": "建议约定：保密义务自签署之日起持续 {n} 年。",
        "conditions": [],
    },
    {
        "code": "FORCE_MAJEURE_NO_NOTICE",
        "name": "不可抗力无通知时效",
        "category": RiskCategory.FORCE_MAJEURE,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_pattern": "日内通知"},
        "result_template": "不可抗力条款未约定通知时效，事后举证易生争议。",
        "suggestion_template": "建议约定：受影响方应在不可抗力发生后 15 日内书面通知对方。",
        "conditions": [],
    },
    {
        "code": "AMOUNT_MISSING",
        "name": "合同金额缺失",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["amount"]},
        "result_template": "未提取到合同金额，影响后续比例类规则判定。",
        "suggestion_template": "建议在合同中以大写与小写并列方式明确合同总金额。",
        "conditions": [_c("metadata.amount", RuleOperator.EXISTS, None)],
    },
    {
        "code": "CURRENCY_MISSING",
        "name": "币种缺失",
        "category": RiskCategory.AMOUNT_PAYMENT,
        "risk_level": RiskLevel.MEDIUM,
        "rule_type": RuleType.PRESENCE,
        "config": {"required_keys": ["currency"]},
        "result_template": "未明确币种，涉外场景下金额存在歧义。",
        "suggestion_template": "建议明确约定合同币种（如人民币 CNY）。",
        "conditions": [_c("metadata.currency", RuleOperator.EXISTS, None)],
    },
]


#: 业务类型 → 该类型下启用的规则。
#: 新增合同类型时**只在这里加一组**，不要在别处复挂其他类型的规则。
RULES_BY_TYPE: dict[str, list[dict]] = {
    BusinessType.PURCHASE: _PURCHASE_RULES,
    BusinessType.SALES: _SALES_RULES,
    BusinessType.LABOR: _LABOR_RULES,
    BusinessType.SERVICE: _SERVICE_RULES,
}

#: 全部规则的扁平视图（供 dry-run 统计与测试遍历）。
RULES: list[dict] = [r for rs in RULES_BY_TYPE.values() for r in rs]


# ==================== 标准示范条款 ====================

STANDARD_CLAUSES: list[dict] = [
    {"clause_type": ClauseType.ACCEPTANCE, "contract_type": None,
     "title": "验收标准与付款前置",
     "content": "乙方交付标的物后，甲方应在 10 个工作日内完成验收并出具书面验收单；"
                "验收合格的，甲方于收到合规发票后 30 日内支付相应款项。"
                "未经甲方书面验收确认，甲方无付款义务。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.LIABILITY, "contract_type": None,
     "title": "责任上限",
     "content": "除因故意或重大过失、侵犯知识产权、违反保密义务外，"
                "任一方在本合同项下承担的赔偿责任总额，不超过本合同总金额。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.LIABILITY, "contract_type": None,
     "title": "违约金对等约定",
     "content": "任何一方违反本合同约定的，应向守约方支付违约金，"
                "违约金比例为合同总金额的 5%，最高不超过合同总金额的 20%。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.JURISDICTION, "contract_type": None,
     "title": "管辖法院",
     "content": "因本合同引起的或与本合同有关的任何争议，双方应友好协商解决；"
                "协商不成的，任何一方均可向我方所在地有管辖权的人民法院提起诉讼。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.INTELLECTUAL_PROPERTY, "contract_type": None,
     "title": "知识产权归属",
     "content": "乙方为履行本合同专门开发、创作的成果，其知识产权归甲方所有；"
                "乙方在本合同签订前已拥有的既有知识产权仍归乙方所有，"
                "但乙方应授予甲方为使用交付成果所必需的许可。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.CONFIDENTIALITY, "contract_type": None,
     "title": "保密期限",
     "content": "双方对因履行本合同而知悉的对方商业秘密与保密信息负有保密义务，"
                "保密期限自本合同签署之日起至保密信息公开之日止，且不少于 3 年。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.FORCE_MAJEURE, "contract_type": None,
     "title": "不可抗力通知时效",
     "content": "因不可抗力不能履行本合同的，受影响方应在不可抗力事件发生后 15 日内"
                "书面通知对方，并在 30 日内提供有权机关出具的证明文件。"
                "未按期通知的，不得据此主张免责。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.DATA_SECURITY, "contract_type": None,
     "title": "数据安全与个人信息保护",
     "content": "乙方处理甲方数据应遵守《中华人民共和国数据安全法》"
                "《中华人民共和国个人信息保护法》等法律法规，"
                "不得超出甲方授权范围处理数据，不得向第三方提供。",
     "source": "《数据安全法》《个人信息保护法》"},
    {"clause_type": ClauseType.PAYMENT, "contract_type": None,
     "title": "分期付款与验收挂钩",
     "content": "合同款项按以下进度支付：合同签订后支付 30% 作为预付款；"
                "全部标的物验收合格后支付 60%；质保期满且无质量问题后支付剩余 10%。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.SUBJECT_MATTER, "contract_type": None,
     "title": "标的物描述",
     "content": "本合同标的物为：{标的物名称}，规格型号：{规格}，数量：{数量}，"
                "质量标准：{标准}。具体以附件《采购清单》为准。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.ACCEPTANCE, "contract_type": BusinessType.PURCHASE,
     "title": "采购验收异议期",
     "content": "甲方在验收过程中发现标的物不符合约定的，应在验收期内书面通知乙方；"
                "乙方应在收到通知后 7 个工作日内完成更换或修复，因此产生的费用由乙方承担。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.LIABILITY, "contract_type": BusinessType.PURCHASE,
     "title": "采购方违约责任",
     "content": "甲方逾期付款的，每逾期一日按应付未付金额的 0.05% 支付违约金，"
                "累计不超过应付未付金额的 10%。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.LIABILITY, "contract_type": BusinessType.SALES,
     "title": "销售方违约责任",
     "content": "乙方逾期交付的，每逾期一日按合同总金额的 0.05% 支付违约金，"
                "累计不超过合同总金额的 10%；逾期超过 30 日的，甲方有权解除合同。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.CONFIDENTIALITY, "contract_type": BusinessType.SERVICE,
     "title": "服务方保密与竞业",
     "content": "乙方及其人员对服务过程中知悉的甲方信息负有保密义务；"
                "服务期内及服务结束后 2 年内，乙方不得利用该信息从事损害甲方利益的活动。",
     "source": "行业惯例"},
    {"clause_type": ClauseType.OTHER, "contract_type": BusinessType.LABOR,
     "title": "劳动合同期限与试用期",
     "content": "本合同为固定期限劳动合同，期限自 {起} 至 {止}，"
                "其中试用期为 {n} 个月，试用期工资不低于本单位相同岗位最低档工资。",
     "source": "《劳动合同法》第十九条、第二十条"},
]


# ==================== 主体黑名单（虚构演示数据）====================

BLACKLIST: list[dict] = [
    {"subject_name": "深圳市鑫达通贸易有限公司", "credit_code": "91440300MA5DEMO001",
     "status": "经营异常", "detail": "通过登记的住所无法联系（演示数据）"},
    {"subject_name": "杭州云创科技有限公司", "credit_code": "91330100MA2DEMO002",
     "status": "严重违法失信", "detail": "被列入严重违法失信企业名单（演示数据）"},
    {"subject_name": "广州汇丰物流有限公司", "credit_code": "91440100MA5DEMO003",
     "status": "注销", "detail": "企业已办理注销登记（演示数据）"},
    {"subject_name": "北京恒信建筑工程有限公司", "credit_code": "91110108MA0DEMO004",
     "status": "经营异常", "detail": "未按规定期限公示年度报告（演示数据）"},
    {"subject_name": "成都天成电子有限公司", "credit_code": "91510100MA6DEMO005",
     "status": "严重违法失信", "detail": "存在重大税收违法记录（演示数据）"},
]


# ==================== 写入逻辑 ====================

def seed_templates(db: Session) -> dict[str, RuleTemplate]:
    """写入规则模板，返回 contract_type -> 模板 的映射。"""
    result: dict[str, RuleTemplate] = {}
    for item in TEMPLATES:
        obj = db.execute(
            select(RuleTemplate).where(RuleTemplate.contract_type == item["contract_type"])
        ).scalar_one_or_none()
        if obj is None:
            obj = RuleTemplate(**item)
            db.add(obj)
            db.flush()  # 取到 id
        else:
            obj.name = item["name"]
            obj.description = item["description"]
        result[item["contract_type"]] = obj
    return result


def seed_rules(db: Session, templates: dict[str, RuleTemplate]) -> int:
    """按业务类型写入规则与条件（`RULES_BY_TYPE`）。

    规则按 `(template_id, code)` 唯一，重复执行走更新分支。

    ⚠️ **改挂模板时必须清理旧挂载点**：批次 9 之前所有规则都挂在采购模板下，
    本函数改为按类型挂载后，采购模板里那些"其实属于销售/劳动"的同名规则
    必须删掉——`(template_id, code)` 唯一索引会让它们作为**孤儿规则**留在
    采购模板里继续生效（如劳动合同的"试用期工资低于法定下限"跑到采购合同上）。
    """
    count = 0
    for bt in (BusinessType.PURCHASE, BusinessType.SALES,
               BusinessType.SERVICE, BusinessType.LABOR):
        tpl = templates[bt]
        rules = RULES_BY_TYPE.get(bt, [])
        keep_codes = {item["code"] for item in rules}

        # 1) 清掉本模板下已不在定义里的规则（改挂/删除的残留）
        for stale in list(db.execute(
            select(Rule).where(Rule.template_id == tpl.id)
        ).scalars()):
            if stale.code not in keep_codes:
                db.delete(stale)
        db.flush()

        # 2) upsert 本类型的规则
        for seq, item in enumerate(rules):
            conditions = item["conditions"]
            payload = {k: v for k, v in item.items() if k != "conditions"}

            rule = db.execute(
                select(Rule).where(Rule.template_id == tpl.id, Rule.code == item["code"])
            ).scalar_one_or_none()
            if rule is None:
                rule = Rule(template_id=tpl.id, seq=seq, **payload)
                db.add(rule)
                db.flush()
            else:
                for k, v in payload.items():
                    setattr(rule, k, v)
                rule.seq = seq
                # 条件整体重建，避免残留已删除的条件
                for old in list(rule.conditions):
                    db.delete(old)
                db.flush()

            for cseq, cond in enumerate(conditions):
                db.add(RuleCondition(rule_id=rule.id, seq=cseq, **cond))
            count += 1
    return count


def seed_standard_clauses(db: Session) -> int:
    """写入标准示范条款，按 (clause_type, contract_type, title) 判重。"""
    count = 0
    for item in STANDARD_CLAUSES:
        obj = db.execute(
            select(StandardClause).where(
                StandardClause.clause_type == item["clause_type"],
                StandardClause.contract_type.is_(None)
                if item["contract_type"] is None
                else StandardClause.contract_type == item["contract_type"],
                StandardClause.title == item["title"],
            )
        ).scalar_one_or_none()
        if obj is None:
            db.add(StandardClause(**item))
            count += 1
    return count


def seed_blacklist(db: Session) -> int:
    """写入主体黑名单，按 subject_name 判重。"""
    count = 0
    for item in BLACKLIST:
        obj = db.execute(
            select(SubjectBlacklist).where(
                SubjectBlacklist.subject_name == item["subject_name"]
            )
        ).scalar_one_or_none()
        if obj is None:
            db.add(SubjectBlacklist(**item))
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="写入种子数据（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    if args.dry_run:
        print("将写入：")
        print(f"  规则模板      {len(TEMPLATES)} 条")
        print(f"  规则          {len(RULES)} 条")
        print(f"  规则条件      {sum(len(r['conditions']) for r in RULES)} 条")
        print(f"  标准示范条款  {len(STANDARD_CLAUSES)} 条")
        print(f"  主体黑名单    {len(BLACKLIST)} 条")
        return 0

    with SessionLocal() as db:
        templates = seed_templates(db)
        n_rules = seed_rules(db, templates)
        n_clauses = seed_standard_clauses(db)
        n_black = seed_blacklist(db)
        db.commit()

        # 回读统计，确认落库
        print("种子数据写入完成：")
        print(f"  规则模板      {db.query(RuleTemplate).count()} 条")
        print(f"  规则          {db.query(Rule).count()} 条（本次处理 {n_rules}）")
        print(f"  规则条件      {db.query(RuleCondition).count()} 条")
        print(f"  标准示范条款  {db.query(StandardClause).count()} 条（新增 {n_clauses}）")
        print(f"  主体黑名单    {db.query(SubjectBlacklist).count()} 条（新增 {n_black}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
