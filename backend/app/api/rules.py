"""规则与知识库路由。

阶段一**只读**（architecture.md §18.1：规则配置页推阶段二）。
提供只读接口是为了：① 前端能展示"这条风险是哪个规则判的"；
② 演示时能解释审查依据。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Rule, RuleTemplate, StandardClause, SubjectBlacklist
from app.schemas import (
    BlacklistOut,
    RuleConditionOut,
    RuleOut,
    RuleTemplateOut,
    StandardClauseOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", tags=["rules"])


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
