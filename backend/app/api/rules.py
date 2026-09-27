"""规则与知识库路由。

阶段一**只读**（architecture.md §18.1）；阶段二批次 7 开放维护
（PRD 2.4.3「维护审查规则项、触发条件、风险分级与标准示范条款库」）。

**写入前必须过 `rule_config.validate_*`**：规则引擎遇到配错的规则会
`logger.error` 后跳过（正确的运行时行为——单条坏规则不该中断整轮审查），
但后果是**配错的规则永久静默失效**。把校验放在写入路径，
让"引擎会忽略"的配置在保存时就变成 400，而不是变成一次没人发现的漏报。

**删除的保护**：`rule` 被 `risk_evidence.rule_id` 引用（外键 `SET NULL`），
`standard_clause` 被防幻觉闸门当法条白名单读。因此规则不做物理删除，
只做 `enabled=0` 停用——删掉会让历史风险项的依据链失去来源，
也会让白名单静默缩小。标准条款可删，但同样先提示影响。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Rule, RuleCondition, RuleTemplate, StandardClause, SubjectBlacklist
from app.models.enums import (
    BusinessType,
    ClauseType,
    MetadataKey,
    RiskCategory,
    RiskLevel,
    RuleOperator,
    RuleType,
    ValueType,
)
from app.schemas import (
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
from app.services.review.rule_config import (
    SUPPORTED_METRICS,
    RuleConfigError,
    RuleSpec,
    validate_rule,
    validate_standard_clause,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])

#: 各枚举的中文标签。选项接口下发用，避免前端硬编码导致漂移。
_RULE_TYPE_LABEL = {
    RuleType.KEYWORD.value: "关键词匹配",
    RuleType.REGEX.value: "正则匹配",
    RuleType.THRESHOLD.value: "阈值比较",
    RuleType.PRESENCE.value: "存在性检查（必备条款）",
    RuleType.BLACKLIST.value: "黑名单匹配",
}
_OPERATOR_LABEL = {
    RuleOperator.CONTAINS.value: "包含",
    RuleOperator.NOT_CONTAINS.value: "不包含",
    RuleOperator.REGEX.value: "正则匹配",
    RuleOperator.GT.value: "大于",
    RuleOperator.GTE.value: "大于等于",
    RuleOperator.LT.value: "小于",
    RuleOperator.LTE.value: "小于等于",
    RuleOperator.EQ.value: "等于",
    RuleOperator.EXISTS.value: "存在",
    RuleOperator.NOT_EXISTS.value: "不存在（要求必须存在）",
}
_VALUE_TYPE_LABEL = {
    ValueType.STRING.value: "字符串",
    ValueType.DECIMAL.value: "数值",
    ValueType.DATE.value: "日期",
    ValueType.REGEX.value: "正则",
    ValueType.LIST.value: "列表",
}
_CATEGORY_LABEL = {
    RiskCategory.SUBJECT_QUALIFICATION.value: "主体资质",
    RiskCategory.AMOUNT_PAYMENT.value: "金额与付款",
    RiskCategory.LIABILITY.value: "违约责任",
    RiskCategory.INTELLECTUAL_PROPERTY.value: "知识产权",
    RiskCategory.JURISDICTION.value: "争议管辖",
    RiskCategory.CONFIDENTIALITY.value: "保密",
    RiskCategory.DATA_SECURITY.value: "数据安全",
    RiskCategory.ACCEPTANCE.value: "验收",
    RiskCategory.FORCE_MAJEURE.value: "不可抗力",
    RiskCategory.OTHER.value: "其他",
}
_RISK_LEVEL_LABEL = {
    RiskLevel.HIGH.value: "高风险",
    RiskLevel.MEDIUM.value: "中风险",
    RiskLevel.LOW.value: "低风险",
}
_CLAUSE_TYPE_LABEL = {
    ClauseType.SUBJECT_MATTER.value: "标的物",
    ClauseType.PAYMENT.value: "付款",
    ClauseType.ACCEPTANCE.value: "验收",
    ClauseType.LIABILITY.value: "违约责任",
    ClauseType.CONFIDENTIALITY.value: "保密",
    ClauseType.DATA_SECURITY.value: "数据安全",
    ClauseType.JURISDICTION.value: "争议管辖",
    ClauseType.INTELLECTUAL_PROPERTY.value: "知识产权",
    ClauseType.FORCE_MAJEURE.value: "不可抗力",
    ClauseType.OTHER.value: "其他",
}
_METADATA_KEY_LABEL = {
    MetadataKey.PARTY_A_NAME.value: "甲方名称",
    MetadataKey.PARTY_B_NAME.value: "乙方名称",
    MetadataKey.PARTY_A_CREDIT_CODE.value: "甲方统一社会信用代码",
    MetadataKey.PARTY_B_CREDIT_CODE.value: "乙方统一社会信用代码",
    MetadataKey.CONTRACT_NO.value: "合同编号",
    MetadataKey.AMOUNT.value: "合同金额",
    MetadataKey.CURRENCY.value: "币种",
    MetadataKey.TERM.value: "履约期限",
    MetadataKey.EFFECTIVE_CONDITION.value: "生效条件",
    MetadataKey.SIGN_DATE.value: "签订日期",
    MetadataKey.SIGN_PLACE.value: "签订地点",
}


def _to_rule_out(r: Rule) -> RuleOut:
    return RuleOut(
        id=r.id, template_id=r.template_id, code=r.code, name=r.name,
        category=r.category, risk_level=r.risk_level, rule_type=r.rule_type,
        config=r.config, result_template=r.result_template,
        suggestion_template=r.suggestion_template, enabled=bool(r.enabled), seq=r.seq,
        conditions=[
            RuleConditionOut(
                id=c.id, seq=c.seq, field=c.field, operator=c.operator,
                value=c.value, value_type=c.value_type,
            )
            for c in sorted(r.conditions, key=lambda x: x.seq)
        ],
    )


def _apply_conditions(rule: Rule, conditions: list[RuleConditionIn]) -> None:
    """整体替换规则的条件。`seq` 由列表下标决定。"""
    for old in list(rule.conditions):
        rule.conditions.remove(old)
    for i, c in enumerate(conditions):
        rule.conditions.append(RuleCondition(
            seq=i, field=c.field, operator=c.operator,
            value=c.value, value_type=c.value_type,
        ))


def _spec_of(body: RuleIn | RuleUpdateIn, existing: Rule | None = None) -> RuleSpec:
    """把请求体（可能是不完整的 PATCH）合并成完整规格再校验。"""
    def pick(attr: str, default=None):
        v = getattr(body, attr, None)
        if v is None:
            v = getattr(existing, attr, None) if existing is not None else default
        return v

    conds = body.conditions
    if conds is None:
        if existing is not None:
            conds_in = [
                {"field": c.field, "operator": c.operator,
                 "value": c.value, "value_type": c.value_type}
                for c in sorted(existing.conditions, key=lambda x: x.seq)
            ]
        else:
            conds_in = []
    else:
        conds_in = [
            {"field": c.field, "operator": c.operator,
             "value": c.value, "value_type": c.value_type}
            for c in conds
        ]

    return RuleSpec(
        code=pick("code", ""), name=pick("name", ""),
        category=pick("category", ""), risk_level=pick("risk_level", ""),
        rule_type=pick("rule_type", ""), config=pick("config"),
        result_template=pick("result_template"),
        suggestion_template=pick("suggestion_template"),
        conditions=conds_in,
    )


# ==================== 选项（前端下拉用） ====================

@router.get("/options", response_model=RuleOptionsOut, summary="规则配置页选项")
def get_options() -> RuleOptionsOut:
    """下发各枚举的取值与中文标签。

    **一次下发而非前端硬编码**：枚举值前后端漂移时，用户能选到后端不认的值，
    保存后规则静默失效。集中下发杜绝这一类问题。
    """
    return RuleOptionsOut(
        rule_type=[{"value": k, "label": v} for k, v in _RULE_TYPE_LABEL.items()],
        operator=[{"value": k, "label": v} for k, v in _OPERATOR_LABEL.items()],
        value_type=[{"value": k, "label": v} for k, v in _VALUE_TYPE_LABEL.items()],
        category=[{"value": k, "label": v} for k, v in _CATEGORY_LABEL.items()],
        risk_level=[{"value": k, "label": v} for k, v in _RISK_LEVEL_LABEL.items()],
        clause_type=[{"value": k, "label": v} for k, v in _CLAUSE_TYPE_LABEL.items()],
        metadata_key=[{"value": k, "label": v} for k, v in _METADATA_KEY_LABEL.items()],
        metric=[{"value": k, "label": v} for k, v in SUPPORTED_METRICS.items()],
    )


# ==================== 模板 ====================

@router.get("/templates", response_model=list[RuleTemplateOut], summary="规则模板列表")
def list_templates(
    db: Session = Depends(get_db),
    contract_type: str | None = Query(default=None, description="按业务类型筛选"),
) -> list[RuleTemplateOut]:
    """规则模板（按合同类型组织），含各自的规则。"""
    stmt = select(RuleTemplate)
    if contract_type:
        stmt = stmt.where(RuleTemplate.contract_type == contract_type)
    templates = list(db.execute(stmt.order_by(RuleTemplate.id)).scalars())
    return [
        RuleTemplateOut(
            id=t.id, contract_type=t.contract_type, name=t.name,
            description=t.description, enabled=bool(t.enabled),
            rules=[_to_rule_out(r) for r in sorted(t.rules, key=lambda x: (x.seq, x.id))],
        )
        for t in templates
    ]


@router.patch(
    "/templates/{template_id}", response_model=RuleTemplateOut, summary="更新规则模板"
)
def update_template(
    template_id: int, body: RuleTemplateUpdateIn, db: Session = Depends(get_db)
) -> RuleTemplateOut:
    """更新模板的名称 / 描述 / 启用状态。"""
    t = db.get(RuleTemplate, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"规则模板不存在: {template_id}")

    if body.name is not None:
        t.name = body.name
    if body.description is not None:
        t.description = body.description
    if body.enabled is not None:
        t.enabled = 1 if body.enabled else 0
    db.commit()
    db.refresh(t)
    logger.info("规则模板 #%s 已更新", template_id)
    return RuleTemplateOut(
        id=t.id, contract_type=t.contract_type, name=t.name,
        description=t.description, enabled=bool(t.enabled),
        rules=[_to_rule_out(r) for r in sorted(t.rules, key=lambda x: (x.seq, x.id))],
    )


# ==================== 规则 ====================

@router.get("/rules", response_model=list[RuleOut], summary="规则列表")
def list_rules(
    db: Session = Depends(get_db),
    template_id: int | None = Query(default=None, description="按模板筛选"),
    enabled_only: bool = Query(default=False, description="只看启用的"),
) -> list[RuleOut]:
    """规则列表。"""
    stmt = select(Rule)
    if template_id is not None:
        stmt = stmt.where(Rule.template_id == template_id)
    if enabled_only:
        stmt = stmt.where(Rule.enabled == 1)
    rules = list(db.execute(stmt.order_by(Rule.template_id, Rule.seq, Rule.id)).scalars())
    return [_to_rule_out(r) for r in rules]


@router.get("/rules/{rule_id}", response_model=RuleOut, summary="规则详情")
def get_rule(rule_id: int, db: Session = Depends(get_db)) -> RuleOut:
    r = db.get(Rule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return _to_rule_out(r)


@router.post("/rules", response_model=RuleOut, status_code=201, summary="新建规则")
def create_rule(body: RuleIn, db: Session = Depends(get_db)) -> RuleOut:
    """新建规则。

    校验不通过返回 **400**（含具体原因），而不是落库后静默失效。
    """
    if db.get(RuleTemplate, body.template_id) is None:
        raise HTTPException(status_code=404, detail=f"规则模板不存在: {body.template_id}")

    try:
        validate_rule(_spec_of(body))
    except RuleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dup = db.execute(
        select(Rule).where(Rule.template_id == body.template_id, Rule.code == body.code)
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=409, detail=f"该模板下已存在编码 {body.code!r} 的规则（#{dup.id}）"
        )

    rule = Rule(
        template_id=body.template_id, code=body.code, name=body.name,
        category=body.category, risk_level=body.risk_level, rule_type=body.rule_type,
        config=body.config, result_template=body.result_template,
        suggestion_template=body.suggestion_template,
        enabled=1 if body.enabled else 0, seq=body.seq,
    )
    db.add(rule)
    db.flush()
    _apply_conditions(rule, body.conditions)
    db.commit()
    db.refresh(rule)
    logger.info("新建规则 #%s %s（模板 #%s）", rule.id, rule.code, rule.template_id)
    return _to_rule_out(rule)


@router.patch("/rules/{rule_id}", response_model=RuleOut, summary="更新规则")
def update_rule(
    rule_id: int, body: RuleUpdateIn, db: Session = Depends(get_db)
) -> RuleOut:
    """更新规则。未提供的字段不改；`conditions` 提供则整体替换。

    **合并后的完整配置再校验**：只改 `config.threshold` 而不管其他字段时，
    也必须拿合并结果去校验，否则会出现"改坏了但没拦住"。
    """
    r = db.get(Rule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")

    if body.code is not None and body.code != r.code:
        dup = db.execute(
            select(Rule).where(
                Rule.template_id == r.template_id,
                Rule.code == body.code,
                Rule.id != rule_id,
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(
                status_code=409, detail=f"该模板下已存在编码 {body.code!r} 的规则（#{dup.id}）"
            )

    try:
        validate_rule(_spec_of(body, existing=r))
    except RuleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for attr in ("code", "name", "category", "risk_level", "rule_type",
                 "config", "result_template", "suggestion_template"):
        v = getattr(body, attr)
        if v is not None:
            setattr(r, attr, v)
    if body.enabled is not None:
        r.enabled = 1 if body.enabled else 0
    if body.seq is not None:
        r.seq = body.seq
    if body.conditions is not None:
        _apply_conditions(r, body.conditions)

    db.commit()
    db.refresh(r)
    logger.info("规则 #%s 已更新", rule_id)
    return _to_rule_out(r)


@router.delete("/rules/{rule_id}", response_model=dict, summary="删除规则")
def delete_rule(
    rule_id: int, db: Session = Depends(get_db), force: bool = Query(default=False)
) -> dict:
    """删除规则。

    **被历史风险项引用时拒绝删除（409）**，除非显式 `force=true`：
    `risk_evidence.rule_id` 是 `ON DELETE SET NULL`，硬删会让历史审查结果的
    "依据链"失去来源，报告里那条依据就说不清是哪条规则判的。
    常规做法是停用（`enabled=0`），保留可追溯性。
    """
    r = db.get(Rule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")

    refs = db.execute(
        select(func.count()).select_from(RuleCondition).where(RuleCondition.rule_id == rule_id)
    ).scalar() or 0

    from app.models import RiskEvidence
    evidence_refs = db.execute(
        select(func.count()).select_from(RiskEvidence).where(RiskEvidence.rule_id == rule_id)
    ).scalar() or 0

    if evidence_refs and not force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"规则 {r.code!r} 已被 {evidence_refs} 条历史风险依据引用，"
                "删除会让依据链失去来源。建议改为停用（enabled=false）；"
                "确需删除请传 force=true"
            ),
        )

    code = r.code
    db.delete(r)
    db.commit()
    logger.warning(
        "规则 #%s %s 已删除（连带 %s 个条件，%s 条依据引用被置空）",
        rule_id, code, refs, evidence_refs,
    )
    return {"ok": True, "deleted_conditions": refs, "detached_evidences": evidence_refs}


# ==================== 标准示范条款 ====================

@router.get("/standard-clauses", response_model=list[StandardClauseOut], summary="标准示范条款库")
def list_standard_clauses(
    db: Session = Depends(get_db),
    clause_type: str | None = Query(default=None),
    contract_type: str | None = Query(default=None),
) -> list[StandardClauseOut]:
    """标准示范条款库。

    本表同时是防幻觉的"法条白名单"：LLM 给出的法条若无法在 `source`
    中匹配，对应依据会标 `need_review`。
    """
    stmt = select(StandardClause)
    if clause_type:
        stmt = stmt.where(StandardClause.clause_type == clause_type)
    if contract_type:
        stmt = stmt.where(
            (StandardClause.contract_type == contract_type)
            | (StandardClause.contract_type.is_(None))
        )
    rows = db.execute(stmt.order_by(StandardClause.clause_type, StandardClause.id)).scalars()
    return [
        StandardClauseOut(
            id=s.id, clause_type=s.clause_type, contract_type=s.contract_type,
            title=s.title, content=s.content, source=s.source, enabled=bool(s.enabled),
        )
        for s in rows
    ]


@router.post(
    "/standard-clauses", response_model=StandardClauseOut, status_code=201,
    summary="新建标准示范条款",
)
def create_standard_clause(
    body: StandardClauseIn, db: Session = Depends(get_db)
) -> StandardClauseOut:
    """新建标准示范条款。"""
    try:
        validate_standard_clause(
            clause_type=body.clause_type, contract_type=body.contract_type,
            title=body.title, content=body.content,
        )
    except RuleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    dup = db.execute(
        select(StandardClause).where(
            StandardClause.clause_type == body.clause_type,
            StandardClause.title == body.title,
            StandardClause.contract_type.is_(None)
            if body.contract_type is None
            else StandardClause.contract_type == body.contract_type,
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已存在同类型同标题的示范条款（#{dup.id}）：{body.title}",
        )

    row = StandardClause(
        clause_type=body.clause_type, contract_type=body.contract_type,
        title=body.title, content=body.content, source=body.source,
        enabled=1 if body.enabled else 0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("新建标准示范条款 #%s %s", row.id, row.title)
    return StandardClauseOut(
        id=row.id, clause_type=row.clause_type, contract_type=row.contract_type,
        title=row.title, content=row.content, source=row.source, enabled=bool(row.enabled),
    )


@router.patch(
    "/standard-clauses/{clause_id}", response_model=StandardClauseOut, summary="更新标准示范条款"
)
def update_standard_clause(
    clause_id: int, body: StandardClauseUpdateIn, db: Session = Depends(get_db)
) -> StandardClauseOut:
    """更新标准示范条款。

    ⚠️ `source` 同时是防幻觉白名单：改动会影响后续审查中法条是否被判"待复核"，
    但**不影响已落库的历史依据**（那些已定格 `need_review`）。
    """
    row = db.get(StandardClause, clause_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"标准条款不存在: {clause_id}")

    new_type = body.clause_type if body.clause_type is not None else row.clause_type
    new_contract = body.contract_type if "contract_type" in body.model_fields_set else row.contract_type
    new_title = body.title if body.title is not None else row.title
    new_content = body.content if body.content is not None else row.content

    try:
        validate_standard_clause(
            clause_type=new_type, contract_type=new_contract,
            title=new_title, content=new_content,
        )
    except RuleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row.clause_type = new_type
    row.contract_type = new_contract
    row.title = new_title
    row.content = new_content
    if body.source is not None:
        row.source = body.source
    if body.enabled is not None:
        row.enabled = 1 if body.enabled else 0

    db.commit()
    db.refresh(row)
    logger.info("标准示范条款 #%s 已更新", clause_id)
    return StandardClauseOut(
        id=row.id, clause_type=row.clause_type, contract_type=row.contract_type,
        title=row.title, content=row.content, source=row.source, enabled=bool(row.enabled),
    )


@router.delete(
    "/standard-clauses/{clause_id}", response_model=dict, summary="删除标准示范条款"
)
def delete_standard_clause(clause_id: int, db: Session = Depends(get_db)) -> dict:
    """删除标准示范条款。"""
    row = db.get(StandardClause, clause_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"标准条款不存在: {clause_id}")
    title = row.title
    db.delete(row)
    db.commit()
    logger.warning("标准示范条款 #%s %s 已删除", clause_id, title)
    return {"ok": True, "title": title}


# ==================== 主体黑名单 ====================

@router.get("/blacklist", response_model=list[BlacklistOut], summary="主体黑名单")
def list_blacklist(db: Session = Depends(get_db)) -> list[BlacklistOut]:
    """主体黑名单。

    ⚠️ **mock 数据，非真实工商信息**（docs/data-model.md §5.18），
    不得用于真实业务判断。前端展示时应带同样提示。
    """
    rows = db.execute(select(SubjectBlacklist).order_by(SubjectBlacklist.id)).scalars()
    return [
        BlacklistOut(
            id=b.id, subject_name=b.subject_name, credit_code=b.credit_code,
            status=b.status, detail=b.detail,
        )
        for b in rows
    ]
