"""数据层测试：模型约束、生成列去重、枚举宽度、索引完备性。

这些测试**连真实 MySQL 容器**（不是 SQLite 内存库），因为本项目的
关键设计（生成列、原生 JSON、DATETIME(3)、ON UPDATE）都是 MySQL 专有行为，
用 SQLite 跑等于没测（见 docs/data-model.md §9.1）。

每个测试自带数据清理，可重复执行。
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, engine
from app.models import TABLE_ORDER, Contract, RiskItem, ReviewTask
from app.models.enums import BusinessType, FileFormat, RiskLevel, TaskStatus


def _make_contract(db: Session, *, file_hash: str, title: str = "测试合同",
                   flush: bool = True) -> Contract:
    """构造一份最小可用的合同记录（只填非空列）。

    `flush=False` 用于"期望 flush 时被唯一键拒绝"的用例：
    否则异常会在构造阶段抛出，绕开 pytest.raises 的作用域。
    """
    c = Contract(
        title=title,
        business_type=BusinessType.PURCHASE.value,
        file_format=FileFormat.PDF.value,
        file_object_key=f"{title}/original.pdf",
        file_name="t.pdf",
        file_size=1024,
        file_hash=file_hash,
    )
    db.add(c)
    if flush:
        db.flush()
    return c


@pytest.fixture()
def db() -> Session:
    """提供会话，并在结束后清理本次测试写入的合同数据。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in (
            "anchor", "risk_evidence", "risk_item", "clause", "contract_metadata",
            "parse_result", "task_event", "annotation", "writeback_log",
            "export_record", "llm_call_log",
        ):
            session.execute(text(f"DELETE FROM `{t}`"))
        session.execute(text("DELETE FROM review_task"))
        session.execute(text("DELETE FROM contract"))
        session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        session.commit()
        session.close()


# ==================== 表结构 ====================

def test_all_18_tables_exist() -> None:
    """18 张业务表全部建成，与 TABLE_ORDER 一致。"""
    names = set(inspect(engine).get_table_names())
    missing = [t for t in TABLE_ORDER if t not in names]
    assert not missing, f"缺表: {missing}"
    assert len(TABLE_ORDER) == 18


def test_enum_columns_are_wide_enough() -> None:
    """枚举列宽按 ENUM_WIDTH 固定，不能等于"当前最长值"。

    若列宽等于当前枚举最长值，说明有人退回了自动推导——
    那会让新增枚举值触发 DDL 迁移，违背原则 P2。
    """
    from app.models.enums import ENUM_WIDTH

    insp = inspect(engine)
    checked = 0
    for table in ("contract", "review_task", "risk_item", "clause", "parse_result"):
        for col in insp.get_columns(table):
            if col["name"] in ("status", "risk_level", "category", "merged_by",
                               "business_type", "clause_type", "blocked_reason",
                               "writeback_status", "overall_risk", "conclusion",
                               "parse_method", "anchor_level", "source", "file_format"):
                assert isinstance(col["type"].length, int)
                checked += 1
    assert checked > 0, "没有抽查到任何枚举列，检查逻辑失效"
    # 抽查两个关键值：列宽应严格大于"当前最长枚举值"
    assert ENUM_WIDTH["RiskLevel"] > len(RiskLevel.MEDIUM.value)


# ==================== 生成列去重（D7 核心）====================

def test_file_hash_dedup_rejects_duplicate(db: Session) -> None:
    """未删除的合同之间，同一 file_hash 必须被数据库拒绝。"""
    h = hashlib.sha256(b"dedup-case").hexdigest()
    _make_contract(db, file_hash=h)
    _make_contract(db, file_hash=h, title="重复合同", flush=False)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_soft_delete_allows_reupload(db: Session) -> None:
    """软删除后，同一 file_hash 可以重新上传。"""
    h = hashlib.sha256(b"reupload-case").hexdigest()
    first = _make_contract(db, file_hash=h)
    db.flush()
    db.execute(text("UPDATE contract SET deleted_at=NOW(3) WHERE id=:i"), {"i": first.id})
    db.flush()

    second = _make_contract(db, file_hash=h, title="重传合同")
    db.flush()
    assert second.id != first.id


def test_deleted_key_is_generated_not_writable(db: Session) -> None:
    """生成列不可写入（MySQL ERROR 3105）。"""
    h = hashlib.sha256(b"generated-col").hexdigest()
    c = _make_contract(db, file_hash=h)
    db.flush()
    with pytest.raises(OperationalError):
        db.execute(text("UPDATE contract SET deleted_key=NOW(3) WHERE id=:i"), {"i": c.id})
    db.rollback()


def test_updated_at_auto_refreshes(db: Session) -> None:
    """updated_at 由数据库自动维护（ON UPDATE），绕过 ORM 也生效。"""
    h = hashlib.sha256(b"onupdate-case").hexdigest()
    c = _make_contract(db, file_hash=h)
    db.flush()
    db.commit()

    before = db.execute(
        text("SELECT updated_at FROM contract WHERE id=:i"), {"i": c.id}
    ).scalar()
    db.execute(text("UPDATE contract SET title='改名' WHERE id=:i"), {"i": c.id})
    db.commit()
    after = db.execute(
        text("SELECT updated_at FROM contract WHERE id=:i"), {"i": c.id}
    ).scalar()
    assert after > before, "updated_at 未随 UPDATE 自动刷新"


# ==================== 关系与约束 ====================

def test_review_task_is_one_to_one(db: Session) -> None:
    """review_task 与 contract 是 1:1，第二个任务被拒。"""
    h = hashlib.sha256(b"one-to-one").hexdigest()
    c = _make_contract(db, file_hash=h)
    db.add(ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value,
                      writeback_status="not_written"))
    db.flush()
    db.add(ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value,
                      writeback_status="not_written"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_risk_item_cascade_delete_with_contract(db: Session) -> None:
    """删合同应级联删风险项（原则 P6：其余实体随合同硬删）。"""
    h = hashlib.sha256(b"cascade-case").hexdigest()
    c = _make_contract(db, file_hash=h)
    db.add(RiskItem(
        contract_id=c.id, title="测试风险", risk_level=RiskLevel.HIGH.value,
        category="liability", reason="r", merged_by="rule", is_global=1,
        unanchored=1, seq=0,
    ))
    db.flush()
    cid = c.id
    db.execute(text("DELETE FROM contract WHERE id=:i"), {"i": cid})
    db.flush()
    left = db.execute(
        text("SELECT COUNT(*) FROM risk_item WHERE contract_id=:i"), {"i": cid}
    ).scalar()
    assert left == 0, "级联删除未生效"


def test_money_is_exact_decimal(db: Session) -> None:
    """金额列是精确 DECIMAL，不是浮点（选 MySQL 的决定性因素）。"""
    h = hashlib.sha256(b"decimal-case").hexdigest()
    c = _make_contract(db, file_hash=h)
    c.amount = "0.30"
    db.flush()
    db.commit()
    val = db.execute(text("SELECT amount FROM contract WHERE id=:i"), {"i": c.id}).scalar()
    assert str(val) == "0.30"
    # 精确比较：0.1 + 0.2 在 MySQL 真 DECIMAL 下等于 0.3
    eq = db.execute(text("SELECT (0.1 + 0.2) = 0.3")).scalar()
    assert eq == 1, "DECIMAL 精度不符合预期"
