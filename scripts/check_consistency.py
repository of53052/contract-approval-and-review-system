"""数据一致性检查：C1 ~ C8。

设计依据：docs/data-model.md §10。

用**应用层脚本**而非数据库触发器兜底：触发器会把逻辑藏在数据库里，
且与 ORM 行为容易冲突（§5.2 原文）。

用法：
    python scripts/check_consistency.py           # 检查全部
    python scripts/check_consistency.py --quiet   # 仅输出结论与违规摘要

退出码：0 = 无违规，1 = 存在违规
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402


@dataclass
class CheckResult:
    """单项检查结果。`rows` 保留违规明细，便于定位。"""

    code: str
    title: str
    violated: int = 0
    rows: list[tuple] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and self.violated == 0


# ==================== 检查实现 ====================

def c1_orphan_anchor(db: Session) -> CheckResult:
    """C1：anchor 的 owner_id 悬空。

    `anchor` 是多态关联，没有数据库外键，孤儿行只能靠本检查发现。
    """
    r = CheckResult("C1", "anchor.owner_id 悬空（孤儿锚点）")
    sql = text("""
        SELECT a.id, a.owner_type, a.owner_id
        FROM anchor a
        LEFT JOIN risk_item ri
               ON a.owner_type = 'risk_item' AND a.owner_id = ri.id
        LEFT JOIN contract_metadata cm
               ON a.owner_type = 'contract_metadata' AND a.owner_id = cm.id
        WHERE (a.owner_type = 'risk_item' AND ri.id IS NULL)
           OR (a.owner_type = 'contract_metadata' AND cm.id IS NULL)
           OR a.owner_type NOT IN ('risk_item', 'contract_metadata')
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c2_missing_anchor(db: Session) -> CheckResult:
    """C2：`risk_item.unanchored = 0` 但不存在任何 anchor（可定位项缺锚点）。"""
    r = CheckResult("C2", "risk_item.unanchored=0 但无 anchor")
    sql = text("""
        SELECT ri.id, ri.title
        FROM risk_item ri
        LEFT JOIN anchor a
               ON a.owner_type = 'risk_item' AND a.owner_id = ri.id
        WHERE ri.unanchored = 0 AND a.id IS NULL
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c3_redundant_counts(db: Session) -> CheckResult:
    """C3：`review_task` 冗余计数 vs `risk_item` 实际条数（不变量 I5）。"""
    r = CheckResult("C3", "review_task 冗余计数与 risk_item 实际条数不一致")
    sql = text("""
        SELECT t.id, t.contract_id,
               t.high_risk_count, t.medium_risk_count, t.low_risk_count,
               COALESCE(SUM(ri.risk_level = 'high'), 0)   AS actual_high,
               COALESCE(SUM(ri.risk_level = 'medium'), 0) AS actual_medium,
               COALESCE(SUM(ri.risk_level = 'low'), 0)    AS actual_low
        FROM review_task t
        LEFT JOIN risk_item ri ON ri.contract_id = t.contract_id
        GROUP BY t.id, t.contract_id,
                 t.high_risk_count, t.medium_risk_count, t.low_risk_count
        HAVING t.high_risk_count   <> COALESCE(SUM(ri.risk_level = 'high'), 0)
            OR t.medium_risk_count <> COALESCE(SUM(ri.risk_level = 'medium'), 0)
            OR t.low_risk_count    <> COALESCE(SUM(ri.risk_level = 'low'), 0)
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c4_blocked_without_reason(db: Session) -> CheckResult:
    """C4：`status = 'blocked'` 但 `blocked_reason` 为空（不变量 I1）。"""
    r = CheckResult("C4", "status=blocked 但 blocked_reason 为空")
    sql = text("""
        SELECT id, contract_id FROM review_task
        WHERE status = 'blocked' AND (blocked_reason IS NULL OR blocked_reason = '')
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c5_completed_incomplete(db: Session) -> CheckResult:
    """C5：`status = 'completed'` 但 `overall_risk` 为空（不变量 I2）。"""
    r = CheckResult("C5", "status=completed 但 overall_risk / conclusion 为空")
    sql = text("""
        SELECT id, contract_id, overall_risk, conclusion FROM review_task
        WHERE status = 'completed'
          AND (overall_risk IS NULL OR overall_risk = ''
               OR conclusion IS NULL OR conclusion = '')
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c6_duplicate_writeback(db: Session) -> CheckResult:
    """C6：同一合同存在多条 `writeback_log.status = 'success'`（重复回写）。"""
    r = CheckResult("C6", "同一合同多条 writeback_log.status=success")
    sql = text("""
        SELECT contract_id, COUNT(*) AS cnt FROM writeback_log
        WHERE status = 'success'
        GROUP BY contract_id HAVING COUNT(*) > 1
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c7_clause_parse_result_mismatch(db: Session) -> CheckResult:
    """C7：`clause.parse_result_id` 不属于同一 `contract_id`（数据串档）。"""
    r = CheckResult("C7", "clause 的 parse_result 属于其他合同")
    sql = text("""
        SELECT c.id, c.contract_id, pr.contract_id AS pr_contract_id
        FROM clause c
        JOIN parse_result pr ON c.parse_result_id = pr.id
        WHERE c.contract_id <> pr.contract_id
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


def c8_bbox_out_of_range(db: Session) -> CheckResult:
    """C8：bbox 坐标越界。

    无页面尺寸表可比对，故按"负值 / 零面积 / 坐标轴倒置"判定：
    这些必然导致前端高亮渲染异常。
    """
    r = CheckResult("C8", "bbox 坐标非法（负值 / 倒置 / 零面积）")
    sql = text("""
        SELECT id, owner_type, owner_id, page_no, bbox_x0, bbox_y0, bbox_x1, bbox_y1
        FROM anchor
        WHERE bbox_x0 < 0 OR bbox_y0 < 0
           OR bbox_x0 >= bbox_x1 OR bbox_y0 >= bbox_y1
    """)
    r.rows = list(db.execute(sql))
    r.violated = len(r.rows)
    return r


CHECKS = [
    c1_orphan_anchor,
    c2_missing_anchor,
    c3_redundant_counts,
    c4_blocked_without_reason,
    c5_completed_incomplete,
    c6_duplicate_writeback,
    c7_clause_parse_result_mismatch,
    c8_bbox_out_of_range,
]


# ==================== 主流程 ====================

def main() -> int:
    parser = argparse.ArgumentParser(description="数据一致性检查 C1~C8")
    parser.add_argument("--quiet", action="store_true", help="仅输出结论")
    parser.add_argument("--max-detail", type=int, default=5, help="每项最多打印几条违规明细")
    args = parser.parse_args()

    results: list[CheckResult] = []
    with SessionLocal() as db:
        for fn in CHECKS:
            try:
                results.append(fn(db))
            except Exception as exc:  # noqa: BLE001 - 检查脚本需捕获全部异常并继续
                r = CheckResult(fn.__name__, fn.__doc__ or "", error=f"{type(exc).__name__}: {exc}")
                results.append(r)

    print("数据一致性检查（C1~C8）")
    print("-" * 56)
    for r in results:
        if r.error:
            mark = "ERROR"
        elif r.passed:
            mark = "PASS "
        else:
            mark = "FAIL "
        print(f"  {mark} {r.code}  {r.title}")
        if r.error:
            print(f"        └─ {r.error}")
        elif not r.passed and not args.quiet:
            for row in r.rows[: args.max_detail]:
                print(f"        └─ {row}")
            if r.violated > args.max_detail:
                print(f"        └─ ... 另有 {r.violated - args.max_detail} 条")

    failed = [r for r in results if not r.passed]
    print("-" * 56)
    if failed:
        print(f"存在违规：{', '.join(r.code for r in failed)}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
