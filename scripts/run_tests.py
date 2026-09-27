# -*- coding: utf-8 -*-
"""回归断言集执行器：跑示例合同，比对人工标注的期望风险点。

设计依据：docs/architecture.md §16.2。

**为什么用应用层脚本而非 pytest**：断言集是"数据驱动"的——
新增一份示例合同只需加一个 `.expected.json`，不改代码。
pytest 更适合断言代码行为，这里断言的是审查质量。

用法：
    python scripts/run_tests.py                # 跑全部示例合同
    python scripts/run_tests.py --keep         # 保留落库数据（便于排查）
    python scripts/run_tests.py --only 设备采购 # 只跑文件名包含该串的样本
    python scripts/run_tests.py --verbose      # 打印逐条比对明细
    python scripts/run_tests.py --verify-anchors
                                               # 附加锚点坐标校验（用渲染用 PDF
                                               # + PDF.js 实测高亮位置，需 node）

退出码：0 = 全部通过，1 = 存在失败

⚠️ 本脚本会清空 `contract` / `review_task` 及其下游表，
**不要与 pytest 并行执行**（两者都会清库，会互相踩踏）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.core.minio_client import get_minio  # noqa: E402
from app.models import (  # noqa: E402
    Anchor,
    Annotation,
    Clause,
    Contract,
    ContractMetadata,
    ExportRecord,
    LlmCallLog,
    ParseResult,
    ReviewTask,
    RiskEvidence,
    RiskItem,
    TaskEvent,
    WritebackLog,
)
from app.models.enums import (  # noqa: E402
    ContractSource,
    FileFormat,
    TaskStatus,
)
from app.services.llm import get_provider  # noqa: E402
from app.workers.pipeline import Pipeline  # noqa: E402

SAMPLES_DIR = PROJECT_ROOT / "samples"
EXPECTED_DIR = SAMPLES_DIR / "expected"

#: 参与比对的字段：规则与 LLM 的措辞不一致，只看 title 会误判漏报。
_HAYSTACK_FIELDS = ("title", "reason", "legal_basis")


@dataclass
class RiskCheck:
    """单个期望风险点的比对结果。"""

    title: str
    matched: bool = False
    actual_title: str | None = None
    level_ok: bool = False
    anchor_ok: bool = False
    #: 锚点 bbox 是否真的覆盖到引用原文（需 --verify-anchors）
    quote_ok: bool | None = None
    #: 实际命中风险项的锚点位置，供坐标校验使用
    page_no: int | None = None
    bbox: list[float] | None = None
    #: 锚点级别是否符合期望（exact / paragraph）
    level_kind_ok: bool = True
    detail: str = ""

    @property
    def passed(self) -> bool:
        # quote_ok 为 None 表示未启用校验，不参与判定
        return (
            self.matched
            and self.level_ok
            and self.anchor_ok
            and self.level_kind_ok
            and self.quote_ok is not False
        )


@dataclass
class CaseResult:
    """一份示例合同的比对结果。"""

    contract: str
    path: Path
    risks: list[RiskCheck] = field(default_factory=list)
    expected_overall: str = ""
    actual_overall: str = ""
    expected_conclusion: str = ""
    actual_conclusion: str = ""
    extra_risks: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if self.actual_overall != self.expected_overall:
            return False
        if self.expected_conclusion and self.actual_conclusion != self.expected_conclusion:
            return False
        return all(r.passed for r in self.risks)


# ==================== 执行 ====================

def load_expected() -> list[dict]:
    """载入全部期望断言文件。"""
    if not EXPECTED_DIR.exists():
        return []
    cases: list[dict] = []
    for p in sorted(EXPECTED_DIR.glob("*.expected.json")):
        cases.append(json.loads(p.read_text(encoding="utf-8")))
    return cases


def resolve_sample(case: dict) -> Path | None:
    """按 sample_dir + contract 定位示例文件。"""
    d = SAMPLES_DIR / case.get("sample_dir", "purchase")
    p = d / case["contract"]
    return p if p.exists() else None


def run_case(
    db: Session, case: dict, *, keep: bool, verify_anchors: bool = False
) -> CaseResult:
    """跑一份示例合同并比对。"""
    import time

    sample = resolve_sample(case)
    if sample is None:
        return CaseResult(
            contract=case["contract"], path=Path(case["contract"]),
            error=f"示例文件不存在: samples/{case.get('sample_dir')}/{case['contract']}",
        )

    result = CaseResult(contract=case["contract"], path=sample)
    raw = sample.read_bytes()

    # 回归用例的 file_hash 加命名空间再哈希，理由有二：
    # ① 不与真实上传/审批同步产生的合同共享 hash —— 测试产物不该遮蔽真实合同的去重；
    # ② 清理时能精确定位本脚本产生的行，**不误删演示数据**
    #    （示例文件与 mock 待办附件是同一份，真实 hash 会撞车）。
    file_hash = hashlib.sha256(f"regression:{sample.name}".encode()).hexdigest()

    # 清理上次运行留下的同名用例（可重复执行）
    _purge_by_hash(db, file_hash)

    fmt = FileFormat.DOCX.value if sample.suffix.lower() == ".docx" else FileFormat.PDF.value
    c = Contract(
        title=case["contract"].rsplit(".", 1)[0],
        business_type=case.get("business_type", "purchase"),
        file_format=fmt, file_object_key=f"regression/{file_hash[:16]}{sample.suffix}",
        file_name=sample.name, file_size=len(raw), file_hash=file_hash,
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()

    t0 = time.perf_counter()
    try:
        pr = Pipeline(db, t, provider=get_provider()).run(sample)
    except Exception as exc:  # noqa: BLE001 - 执行失败如实记录，不掩盖
        result.error = f"流水线异常: {type(exc).__name__}: {exc}"
        return result
    result.duration_ms = int((time.perf_counter() - t0) * 1000)

    if pr.status != TaskStatus.COMPLETED.value:
        result.error = f"任务未完成: status={pr.status} blocked={pr.blocked_reason}"
        return result

    db.refresh(t)
    result.actual_overall = t.overall_risk or ""
    result.actual_conclusion = t.conclusion or ""
    result.expected_overall = case["expected_overall"]
    result.expected_conclusion = case.get("expected_conclusion", "")

    # 载入实际风险项
    risks = list(db.execute(
        select(RiskItem).where(RiskItem.contract_id == c.id).order_by(RiskItem.seq)
    ).scalars())

    # 锚点走多态关联，RiskItem 故意没有 relationship，需单独查
    anchor_map: dict[int, list[Anchor]] = {}
    for a in db.execute(
        select(Anchor)
        .where(Anchor.owner_type == "risk_item", Anchor.owner_id.in_([r.id for r in risks]))
        .order_by(Anchor.owner_id, Anchor.seq)
    ).scalars():
        anchor_map.setdefault(a.owner_id, []).append(a)

    used: set[int] = set()
    for exp in case["expected_risks"]:
        check = _match_one(exp, risks, anchor_map, used)
        result.risks.append(check)

    # 锚点坐标校验：拿**渲染用的那份 PDF** 实测 bbox 是否覆盖引用原文。
    # 这是后端锚点 → 前端高亮的契约边界，坐标约定错了这里就该红。
    if verify_anchors:
        quote_ok = _verify_anchors_by_render(db, c, result.risks, case)
        for check in result.risks:
            check.quote_ok = quote_ok.get(check.title)

    result.extra_risks = [r.title for r in risks if r.id not in used]

    if not keep:
        _purge_by_hash(db, file_hash)
    return result


def _match_one(
    exp: dict,
    risks: list[RiskItem],
    anchor_map: dict[int, list[Anchor]],
    used: set[int],
) -> RiskCheck:
    """把一个期望风险点匹配到实际风险项。"""
    keywords = exp.get("match_keywords") or [exp["title"]]
    check = RiskCheck(title=exp["title"])

    hit: RiskItem | None = None
    for r in risks:
        if r.id in used:
            continue
        haystack = " ".join(str(getattr(r, f) or "") for f in _HAYSTACK_FIELDS)
        if any(kw in haystack for kw in keywords):
            hit = r
            break

    if hit is None:
        check.detail = f"未识别（关键词 {keywords} 未命中任何风险项）"
        return check

    used.add(hit.id)
    check.matched = True
    check.actual_title = hit.title
    check.level_ok = hit.risk_level == exp["risk_level"]

    if exp.get("must_anchor"):
        anchors = anchor_map.get(hit.id, [])
        anchor = anchors[0] if anchors else None
        if anchor is not None:
            check.page_no = anchor.page_no
            check.bbox = [anchor.bbox_x0, anchor.bbox_y0, anchor.bbox_x1, anchor.bbox_y1]
            # 锚点级别由规则类型决定：能提取命中子串的是 exact，
            # PRESENCE 类规则无子串可提取，降级为 paragraph（只覆盖条款标题块）
            want_level = exp.get("expected_anchor_level")
            if want_level and anchor.anchor_level != want_level:
                check.level_kind_ok = False
                check.detail = (
                    f"锚点级别 {anchor.anchor_level} != 期望 {want_level}"
                )
        if anchor is None:
            check.anchor_ok = False
            check.detail = "无锚点"
        elif exp.get("expected_anchor_page") is not None:
            check.anchor_ok = anchor.page_no == exp["expected_anchor_page"]
            if not check.anchor_ok:
                check.detail = f"锚点页码 {anchor.page_no} != 期望 {exp['expected_anchor_page']}"
        else:
            check.anchor_ok = True
    else:
        check.anchor_ok = True

    if not check.level_ok:
        check.detail = f"等级 {hit.risk_level} != 期望 {exp['risk_level']}"
    return check


def _verify_anchors_by_render(
    db: Session, contract: Contract, checks: list[RiskCheck], case: dict
) -> dict[str, bool]:
    """用渲染用的 PDF + PDF.js 实测锚点坐标，返回 {期望标题: 是否覆盖引用}。

    为什么绕到 Node：后端锚点由 PyMuPDF 产生，用 PyMuPDF 自校验是同源验证，
    测不出"前端换算公式错"。这里复用前端组件的同一份换算实现（anchorCoords.ts）。
    """
    import json
    import subprocess
    import tempfile

    node = shutil.which("node")
    script = PROJECT_ROOT / "frontend" / "scripts" / "verify_anchors.mjs"
    if node is None or not script.exists():
        logger.warning("跳过锚点坐标校验（node 或校验脚本缺失）")
        return {}

    # 取渲染用 PDF：DOCX 用转换产物，PDF 用原件
    key = contract.pdf_object_key if contract.file_format == "docx" else contract.file_object_key
    if not key:
        logger.warning("合同 #%s 无渲染用 PDF，跳过锚点校验", contract.id)
        return {}

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "render.pdf"
        resp = get_minio().get_object(settings.minio_bucket_contracts, key)
        try:
            pdf_path.write_bytes(resp.read())
        finally:
            resp.close()
            resp.release_conn()

        # 组装锚点数据：只校验匹配上的风险项，且要求标注了 anchor_quote
        quote_map = {e["title"]: e.get("anchor_quote") for e in case["expected_risks"]}
        payload = []
        for check in checks:
            if not check.matched or not quote_map.get(check.title):
                continue
            payload.append({
                "title": check.title,
                "quote": quote_map[check.title],
                # 锚点数据由 _match_one 已确定，这里从 DB 重取保证是最新的
                "page": check.page_no,
                "bbox": check.bbox,
            })
        if not payload:
            return {}

        anchors_path = Path(tmp) / "anchors.json"
        anchors_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        proc = subprocess.run(
            [node, str(script), str(pdf_path), str(anchors_path)],
            cwd=str(PROJECT_ROOT / "frontend"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            logger.warning("锚点坐标校验未全通过:\n%s", proc.stdout)
        return _parse_verify_output(proc.stdout)


def _parse_verify_output(out: str) -> dict[str, bool]:
    """解析校验脚本输出：以 ✓/✗ 开头的行，取标题与结果。"""
    result: dict[str, bool] = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("\u2713 ") or line.startswith("\u2717 "):
            ok = line.startswith("\u2713")
            # 形如 "✓ [p1] 标题"
            title = line.split("]", 1)[-1].strip() if "]" in line else line[2:].strip()
            result[title] = ok
    return result


def _purge_by_hash(db: Session, file_hash: str) -> None:
    """删除同 file_hash 的合同及其全部下游数据（含软删除）。"""
    rows = db.execute(
        select(Contract.id).where(Contract.file_hash == file_hash)
    ).scalars().all()
    if not rows:
        return
    ids = list(rows)

    # anchor 是多态关联（无数据库外键），先按 risk_item 反查再删
    risk_ids = list(db.execute(
        select(RiskItem.id).where(RiskItem.contract_id.in_(ids))
    ).scalars())
    if risk_ids:
        db.execute(delete(Anchor).where(Anchor.owner_id.in_(risk_ids)))
        db.execute(delete(RiskEvidence).where(RiskEvidence.risk_item_id.in_(risk_ids)))

    # 按 contract_id 关联的表，依赖顺序：叶子 → 根
    for model in (RiskItem, Annotation, Clause, ContractMetadata,
                  ParseResult, WritebackLog, ExportRecord):
        db.execute(delete(model).where(model.contract_id.in_(ids)))

    # task_event / llm_call_log 走 task_id
    task_ids = list(db.execute(
        select(ReviewTask.id).where(ReviewTask.contract_id.in_(ids))
    ).scalars())
    if task_ids:
        db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
        db.execute(delete(LlmCallLog).where(LlmCallLog.task_id.in_(task_ids)))

    db.execute(delete(ReviewTask).where(ReviewTask.contract_id.in_(ids)))
    db.execute(delete(Contract).where(Contract.id.in_(ids)))
    db.commit()


# ==================== 输出 ====================

def print_case(r: CaseResult, *, verbose: bool) -> None:
    """打印单份合同结果。"""
    mark = "✓ 通过" if r.passed else "✗ 失败"
    print(f"\n{'=' * 72}")
    print(f"{mark}  {r.contract}  ({r.duration_ms} ms)")
    print(f"{'=' * 72}")

    if r.error:
        print(f"  错误: {r.error}")
        return

    print(f"  综合风险: 实际 {r.actual_overall or '-'} / 期望 {r.expected_overall}"
          f"   结论: 实际 {r.actual_conclusion or '-'} / 期望 {r.expected_conclusion or '-'}")
    for c in r.risks:
        flag = "✓" if c.passed else "✗"
        line = f"  [{flag}] {c.title}"
        if c.matched:
            line += f"  → 实际「{c.actual_title}」"
            if c.quote_ok is not None:
                line += f"  [锚点坐标{'✓' if c.quote_ok else '✗'}]"
            if (
                not c.level_ok
                or not c.anchor_ok
                or not c.level_kind_ok
                or c.quote_ok is False
            ):
                line += f"  ({c.detail or '锚点未覆盖引用原文'})"
        else:
            line += f"  ({c.detail})"
        print(line)
    if r.extra_risks:
        print(f"  [i] 额外识别 {len(r.extra_risks)} 项（不计入失败）: "
              f"{'、'.join(r.extra_risks)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="回归断言集执行器")
    ap.add_argument("--keep", action="store_true", help="保留落库数据")
    ap.add_argument("--only", default=None, help="只跑文件名包含该串的样本")
    ap.add_argument("--verbose", action="store_true", help="打印逐条明细")
    ap.add_argument(
        "--verify-anchors",
        action="store_true",
        help="用渲染用 PDF + PDF.js 实测锚点坐标是否覆盖引用原文（需 node）",
    )
    args = ap.parse_args()

    cases = load_expected()
    if not cases:
        print("未找到任何期望断言文件（samples/expected/*.expected.json）")
        return 1
    if args.only:
        cases = [c for c in cases if args.only in c["contract"]]
        if not cases:
            print(f"没有匹配 --only {args.only} 的样本")
            return 1

    print(f"回归断言集：共 {len(cases)} 份示例合同")
    db = SessionLocal()
    results: list[CaseResult] = []
    try:
        for case in cases:
            r = run_case(
                db, case, keep=args.keep, verify_anchors=args.verify_anchors
            )
            results.append(r)
            print_case(r, verbose=args.verbose)
    finally:
        db.close()

    passed = sum(1 for r in results if r.passed)
    print(f"\n{'=' * 72}")
    print(f"结果: {passed}/{len(results)} 通过")
    for r in results:
        if not r.passed:
            print(f"  ✗ {r.contract}" + (f" — {r.error}" if r.error else ""))
    print(f"{'=' * 72}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
