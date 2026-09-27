"""风险路由：清单 / 详情 / 编辑建议 / 批注。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import Anchor, Annotation, Contract, RiskEvidence, RiskItem
from app.schemas import (
    AnchorOut,
    AnnotationIn,
    AnnotationOut,
    EvidenceOut,
    RiskItemOut,
    RiskUpdateIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/risks", tags=["risks"])


def _to_risk_out(
    r: RiskItem,
    anchors: list[Anchor],
    evidences: list[RiskEvidence],
) -> RiskItemOut:
    return RiskItemOut(
        id=r.id, title=r.title, risk_level=r.risk_level, category=r.category,
        reason=r.reason, legal_basis=r.legal_basis, suggestion=r.suggestion,
        suggestion_edited=r.suggestion_edited, adopted=bool(r.adopted),
        merged_by=r.merged_by, is_global=bool(r.is_global),
        unanchored=bool(r.unanchored), seq=r.seq, clause_id=r.clause_id,
        anchors=[
            AnchorOut(
                id=a.id, page_no=a.page_no, bbox_x0=a.bbox_x0, bbox_y0=a.bbox_y0,
                bbox_x1=a.bbox_x1, bbox_y1=a.bbox_y1, char_start=a.char_start,
                char_end=a.char_end, quote_text=a.quote_text, source=a.source,
                anchor_level=a.anchor_level, confidence=a.confidence,
            )
            for a in anchors
        ],
        evidences=[
            EvidenceOut(
                id=e.id, evidence_type=e.evidence_type, title=e.title,
                detail=e.detail, raw_snippet=e.raw_snippet, rule_id=e.rule_id,
                need_review=bool(e.need_review),
            )
            for e in evidences
        ],
    )


@router.get("", response_model=list[RiskItemOut], summary="风险清单")
def list_risks(
    db: Session = Depends(get_db),
    contract_id: int = Query(description="合同 ID"),
    level: str | None = Query(default=None, description="按风险等级筛选"),
    category: str | None = Query(default=None, description="按风险分类筛选"),
) -> list[RiskItemOut]:
    """合同的风险清单（工作台右栏）。

    一次查完锚点与依据，**避免 N+1**——风险项通常十几条，
    逐条查锚点会打出几十次查询。
    """
    _ensure_contract(db, contract_id)

    stmt = select(RiskItem).where(RiskItem.contract_id == contract_id)
    if level:
        stmt = stmt.where(RiskItem.risk_level == level)
    if category:
        stmt = stmt.where(RiskItem.category == category)
    risks = list(db.execute(stmt.order_by(RiskItem.seq)).scalars())
    if not risks:
        return []

    ids = [r.id for r in risks]
    anchors: dict[int, list[Anchor]] = {}
    for a in db.execute(
        select(Anchor).where(
            Anchor.owner_type == "risk_item", Anchor.owner_id.in_(ids)
        ).order_by(Anchor.owner_id, Anchor.seq)
    ).scalars():
        anchors.setdefault(a.owner_id, []).append(a)
    evidences: dict[int, list[RiskEvidence]] = {}
    for e in db.execute(
        select(RiskEvidence).where(RiskEvidence.risk_item_id.in_(ids))
    ).scalars():
        evidences.setdefault(e.risk_item_id, []).append(e)

    return [
        _to_risk_out(r, anchors.get(r.id, []), evidences.get(r.id, []))
        for r in risks
    ]


@router.get("/{risk_id}", response_model=RiskItemOut, summary="风险详情")
def get_risk(risk_id: int, db: Session = Depends(get_db)) -> RiskItemOut:
    r = db.get(RiskItem, risk_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"风险项不存在: {risk_id}")
    anchors = list(db.execute(
        select(Anchor).where(
            Anchor.owner_type == "risk_item", Anchor.owner_id == r.id
        ).order_by(Anchor.seq)
    ).scalars())
    evidences = list(db.execute(
        select(RiskEvidence).where(RiskEvidence.risk_item_id == r.id)
    ).scalars())
    return _to_risk_out(r, anchors, evidences)


@router.patch("/{risk_id}", response_model=RiskItemOut, summary="编辑风险项（法务）")
def update_risk(
    risk_id: int, body: RiskUpdateIn, db: Session = Depends(get_db)
) -> RiskItemOut:
    """法务编辑修改建议 / 标记采纳。

    **不变量 I4**：`suggestion_edited` 非空 ⟹ `adopted = 1`
    （docs/data-model.md §5.6）。这里显式维护它，不依赖调用方传对。
    """
    r = db.get(RiskItem, risk_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"风险项不存在: {risk_id}")

    if body.suggestion_edited is not None:
        # 空串视为清空（回退到 AI 原始建议）
        r.suggestion_edited = body.suggestion_edited or None
    if body.adopted is not None:
        r.adopted = 1 if body.adopted else 0

    # 维护 I4：有编辑版建议时必然视为已采纳
    if r.suggestion_edited:
        r.adopted = 1

    db.commit()
    db.refresh(r)
    logger.info("风险项 #%s 已更新（adopted=%s）", risk_id, r.adopted)

    anchors = list(db.execute(
        select(Anchor).where(
            Anchor.owner_type == "risk_item", Anchor.owner_id == r.id
        ).order_by(Anchor.seq)
    ).scalars())
    evidences = list(db.execute(
        select(RiskEvidence).where(RiskEvidence.risk_item_id == r.id)
    ).scalars())
    return _to_risk_out(r, anchors, evidences)


@router.post("/annotations", response_model=AnnotationOut, summary="新增法务批注")
def create_annotation(
    body: AnnotationIn, db: Session = Depends(get_db)
) -> AnnotationOut:
    """新增法务批注。

    批注**不随重试清理**（§6.2）：重试解析时 `annotation` 表保留，
    被删风险项关联的批注会 `SET NULL` 变成整体意见，而不是被连带删除。
    """
    if body.risk_item_id is not None:
        r = db.get(RiskItem, body.risk_item_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"风险项不存在: {body.risk_item_id}")
        contract_id = r.contract_id
    else:
        raise HTTPException(
            status_code=400, detail="必须提供 risk_item_id（阶段一不支持纯整体批注）"
        )

    ann = Annotation(
        contract_id=contract_id, risk_item_id=body.risk_item_id,
        author=body.author, author_role=body.author_role, content=body.content,
    )
    db.add(ann)
    db.commit()
    db.refresh(ann)
    logger.info("批注已创建: #%s by %s", ann.id, ann.author)
    return AnnotationOut(
        id=ann.id, contract_id=ann.contract_id, risk_item_id=ann.risk_item_id,
        author=ann.author, author_role=ann.author_role, content=ann.content,
        created_at=ann.created_at,
    )


@router.get("/annotations/list", response_model=list[AnnotationOut], summary="批注列表")
def list_annotations(
    db: Session = Depends(get_db),
    contract_id: int = Query(description="合同 ID"),
) -> list[AnnotationOut]:
    """合同下的全部批注。"""
    _ensure_contract(db, contract_id)
    rows = db.execute(
        select(Annotation).where(Annotation.contract_id == contract_id).order_by(Annotation.id)
    ).scalars()
    return [
        AnnotationOut(
            id=a.id, contract_id=a.contract_id, risk_item_id=a.risk_item_id,
            author=a.author, author_role=a.author_role, content=a.content,
            created_at=a.created_at,
        )
        for a in rows
    ]


def _ensure_contract(db: Session, contract_id: int) -> Contract:
    c = db.get(Contract, contract_id)
    if c is None or c.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"合同不存在: {contract_id}")
    return c
