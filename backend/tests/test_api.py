"""后端 API 集成测试（FastAPI TestClient，连真实基础设施）。

设计依据：docs/architecture.md §16.1（外部集成最小化 Mock）。

**为什么连真实 MySQL / MinIO / mock 审批服务**：本项目的关键行为是
"跨进程协作"——合同落库 + 对象存储 + 后台线程跑流水线 + 回写外部系统。
用 mock 替掉它们，测出来的只是 mock 自己的行为。

**不测什么**：不重复 test_review_engine.py 已覆盖的引擎逻辑
（规则判定、取高合并、锚点对齐），这里只测 HTTP 契约与端到端链路。

前置：MySQL / Redis / MinIO 容器已启动；mock 审批服务已启动（否则相关用例 skip）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from sqlalchemy import select

from app.core.database import SessionLocal
from app.main import app
from app.models import Contract, ReviewTask, RiskItem, Rule, RuleTemplate
from app.models.enums import (
    ContractSource,
    FileFormat,
    TaskStatus,
)
from app.services.approval import get_adapter

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCX = PROJECT_ROOT / "samples" / "purchase" / "设备采购合同-高风险样本.docx"


# ==================== 夹具 ====================

@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def db():
    """会话 + 用例后清理业务数据。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in (
            "anchor", "risk_evidence", "risk_item", "annotation", "clause",
            "contract_metadata", "parse_result", "task_event", "writeback_log",
            "export_record", "llm_call_log",
        ):
            session.execute(text(f"DELETE FROM `{t}`"))
        session.execute(text("DELETE FROM review_task"))
        session.execute(text("DELETE FROM contract"))
        session.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        session.commit()
        session.close()


def _approval_available() -> bool:
    try:
        ok, _ = get_adapter().health()
        return ok
    except Exception:  # noqa: BLE001
        return False


def _wps_available() -> bool:
    try:
        from app.services.parsing.docx_converter import WpsComConverter
        return WpsComConverter().is_available()
    except Exception:  # noqa: BLE001
        return False


# ==================== 元信息 ====================

def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_meta_config_exposes_no_credentials(client: TestClient) -> None:
    """运行时配置接口**绝不能**泄露凭据（架构 §15.1）。"""
    r = client.get("/api/meta/config")
    assert r.status_code == 200
    body = r.text.lower()
    for forbidden in ("password", "secret", "api_key", "access_key", "token"):
        assert forbidden not in body, f"配置接口泄露了 {forbidden}"


# ==================== 合同 ====================

def test_list_contracts_returns_page(client: TestClient, db) -> None:
    r = client.get("/api/contracts")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "page", "page_size"}
    assert isinstance(body["items"], list)


def test_get_missing_contract_returns_404(client: TestClient) -> None:
    assert client.get("/api/contracts/99999999").status_code == 404


def test_upload_rejects_empty_file(client: TestClient) -> None:
    r = client.post(
        "/api/contracts/upload",
        files={"file": ("empty.docx", b"", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_upload_rejects_unsupported_format(client: TestClient) -> None:
    r = client.post(
        "/api/contracts/upload",
        files={"file": ("evil.exe", b"MZ\x00\x01", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_pdf_endpoint_404_for_missing_contract(client: TestClient) -> None:
    assert client.get("/api/contracts/99999999/pdf").status_code == 404


def test_delete_contract_is_soft(client: TestClient, db) -> None:
    """软删除：列表不再返回，但行仍在库里（原则 P6）。"""
    h = hashlib.sha256(b"soft-delete-case").hexdigest()
    c = Contract(
        title="待删合同", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=h, source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.commit()
    cid = c.id

    assert client.delete(f"/api/contracts/{cid}").status_code == 200
    assert client.get(f"/api/contracts/{cid}").status_code == 404

    db.expire_all()
    row = db.get(Contract, cid)
    assert row is not None, "软删除不应物理删除行"
    assert row.deleted_at is not None


# ==================== 规则库（只读） ====================

@pytest.mark.parametrize(
    "path",
    [
        "/api/rules/templates",
        "/api/rules/rules",
        "/api/rules/standard-clauses",
        "/api/rules/blacklist",
    ],
)
def test_rules_readonly_endpoints(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ==================== 规则配置（批次 7：写入路径）====================

def test_rule_options_expose_all_enums(client: TestClient) -> None:
    """选项接口必须覆盖前端要用的全部枚举，否则用户选不到值。"""
    r = client.get("/api/rules/options")
    assert r.status_code == 200
    body = r.json()
    for key in ("rule_type", "operator", "value_type", "category",
                "risk_level", "clause_type", "metadata_key", "metric"):
        assert body[key], f"{key} 选项为空"
    assert {o["value"] for o in body["risk_level"]} == {"high", "medium", "low"}


def test_create_rule_rejects_invalid_config(client: TestClient, db) -> None:
    """配错的规则必须 400，而不是落库后静默失效。"""
    tpl = db.execute(select(RuleTemplate).limit(1)).scalar_one_or_none()
    if tpl is None:
        pytest.skip("规则模板未初始化（需先跑 scripts/seed_data.py）")

    r = client.post("/api/rules/rules", json={
        "template_id": tpl.id, "code": "BAD_METRIC_RULE", "name": "坏规则",
        "category": "liability", "risk_level": "high", "rule_type": "threshold",
        "config": {"metric": "not_a_metric", "threshold": 0.2},
        "conditions": [],
    })
    assert r.status_code == 400
    assert "metric" in r.json()["detail"]

    # 落库失败：库里不应留下这条规则
    assert db.execute(
        select(Rule).where(Rule.code == "BAD_METRIC_RULE")
    ).scalar_one_or_none() is None


def test_rule_crud_roundtrip(client: TestClient, db) -> None:
    """新建 → 读取 → 更新 → 停用 → 删除 的完整闭环。"""
    tpl = db.execute(select(RuleTemplate).limit(1)).scalar_one_or_none()
    if tpl is None:
        pytest.skip("规则模板未初始化")

    payload = {
        "template_id": tpl.id, "code": "TEST_ROUNDTRIP", "name": "闭环用例规则",
        "category": "confidentiality", "risk_level": "medium", "rule_type": "keyword",
        "config": {"keywords": ["测试关键词"], "match_all": False},
        "result_template": "命中：{clause_no}", "suggestion_template": "建议修改",
        "enabled": True, "seq": 99,
        "conditions": [{"field": "clause.content", "operator": "contains",
                        "value": "测试关键词", "value_type": "string"}],
    }
    r = client.post("/api/rules/rules", json=payload)
    assert r.status_code == 201, r.text
    rule_id = r.json()["id"]
    assert r.json()["conditions"][0]["seq"] == 0, "条件 seq 应由后端按下标生成"

    # 更新：只改名称，其余字段保持
    r = client.patch(f"/api/rules/rules/{rule_id}", json={"name": "改名后"})
    assert r.status_code == 200
    assert r.json()["name"] == "改名后"
    assert r.json()["code"] == "TEST_ROUNDTRIP", "未提供的字段不应被清空"
    assert len(r.json()["conditions"]) == 1

    # 整体替换条件
    r = client.patch(f"/api/rules/rules/{rule_id}", json={
        "conditions": [
            {"field": "clause.content", "operator": "contains", "value": "A"},
            {"field": "metadata.amount", "operator": "exists"},
        ],
    })
    assert r.status_code == 200
    assert [c["seq"] for c in r.json()["conditions"]] == [0, 1]

    # 停用
    r = client.patch(f"/api/rules/rules/{rule_id}", json={"enabled": False})
    assert r.json()["enabled"] is False

    assert client.delete(f"/api/rules/rules/{rule_id}").status_code == 200
    assert client.get(f"/api/rules/rules/{rule_id}").status_code == 404


def test_duplicate_rule_code_rejected(client: TestClient, db) -> None:
    """同一模板内编码唯一。"""
    tpl = db.execute(select(RuleTemplate).limit(1)).scalar_one_or_none()
    existing = db.execute(
        select(Rule).where(Rule.template_id == (tpl.id if tpl else 0)).limit(1)
    ).scalar_one_or_none()
    if existing is None:
        pytest.skip("规则库未初始化")

    r = client.post("/api/rules/rules", json={
        "template_id": existing.template_id, "code": existing.code,
        "name": "重复编码", "category": "liability", "risk_level": "high",
        "rule_type": "keyword", "config": {"keywords": ["x"]}, "conditions": [],
    })
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]


def test_delete_rule_refused_when_referenced(client: TestClient, db) -> None:
    """被历史风险依据引用的规则不得静默删除（依据链会失去来源）。

    **用例自建数据**：不依赖库里恰好存在历史风险项——
    测试夹具每条用例后都会清空业务表，跨用例依赖必然变得 flaky。
    """
    from app.models import RiskEvidence, RiskItem

    tpl = db.execute(select(RuleTemplate).limit(1)).scalar_one_or_none()
    if tpl is None:
        pytest.skip("规则模板未初始化")

    r = client.post("/api/rules/rules", json={
        "template_id": tpl.id, "code": "TEST_REFERENCED", "name": "被引用规则",
        "category": "liability", "risk_level": "high", "rule_type": "keyword",
        "config": {"keywords": ["引用"]}, "conditions": [],
    })
    assert r.status_code == 201
    rule_id = r.json()["id"]

    c = Contract(
        title="依据引用用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"rule-ref").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    ri = RiskItem(
        contract_id=c.id, title="测试风险", risk_level="high", category="other",
        reason="r", is_global=1, unanchored=1, seq=0, merged_by="rule",
    )
    db.add(ri)
    db.flush()
    db.add(RiskEvidence(
        risk_item_id=ri.id, evidence_type="rule", rule_id=rule_id, detail="依据",
    ))
    db.commit()

    r = client.delete(f"/api/rules/rules/{rule_id}")
    assert r.status_code == 409
    assert "历史风险依据" in r.json()["detail"]

    # force 才允许删除
    assert client.delete(f"/api/rules/rules/{rule_id}", params={"force": True}).status_code == 200


def test_template_toggle_affects_rule_loading(db) -> None:
    """模板停用后，Pipeline 不应再加载该类型的规则。"""
    from app.workers.pipeline import Pipeline

    tpl = db.execute(
        select(RuleTemplate).where(RuleTemplate.contract_type == "purchase")
    ).scalar_one_or_none()
    if tpl is None:
        pytest.skip("采购模板未初始化")

    original = tpl.enabled
    try:
        tpl.enabled = 0
        db.commit()
        # _load_rules 只依赖 self.db 与 business_type，构造轻量实例即可
        p = Pipeline.__new__(Pipeline)
        p.db = db
        assert p._load_rules("purchase") == [], "模板停用后不应加载任何规则"
    finally:
        tpl.enabled = original
        db.commit()


def test_standard_clause_crud(client: TestClient, db) -> None:
    """示范条款：新建 / 更新 / 删除，且来源字段影响防幻觉白名单。"""
    from app.models import StandardClause

    payload = {
        "clause_type": "liability", "contract_type": None,
        "title": "批次7测试示范条款", "content": "任一方赔偿总额不超过合同总金额。",
        "source": "《民法典》第五百八十五条", "enabled": True,
    }
    r = client.post("/api/rules/standard-clauses", json=payload)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    # 同类型同标题判重
    assert client.post("/api/rules/standard-clauses", json=payload).status_code == 409

    # 正文为空应 400
    r = client.patch(f"/api/rules/standard-clauses/{cid}", json={"content": "  "})
    assert r.status_code == 400

    r = client.patch(f"/api/rules/standard-clauses/{cid}",
                     json={"title": "改后的标题", "enabled": False})
    assert r.status_code == 200
    assert r.json()["title"] == "改后的标题"
    assert r.json()["enabled"] is False
    assert r.json()["source"] == "《民法典》第五百八十五条", "未提供的字段不应被清空"

    assert client.delete(f"/api/rules/standard-clauses/{cid}").status_code == 200
    assert db.execute(
        select(StandardClause).where(StandardClause.id == cid)
    ).scalar_one_or_none() is None


# ==================== 风险与批注 ====================

def test_risk_list_requires_contract_id(client: TestClient) -> None:
    """缺少必填查询参数应被 FastAPI 拦下（422）。"""
    assert client.get("/api/risks").status_code == 422


def test_risk_patch_maintains_invariant_i4(client: TestClient, db) -> None:
    """不变量 I4：`suggestion_edited` 非空 ⟹ `adopted = 1`（数据模型 §5.6）。"""
    c = Contract(
        title="不变量用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"invariant-i4").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    risk = RiskItem(
        contract_id=c.id, title="测试风险", risk_level="high", category="other",
        reason="r", merged_by="rule", is_global=1, unanchored=1, seq=0,
    )
    db.add(risk)
    db.commit()
    rid = risk.id

    # 只传 suggestion_edited，不传 adopted —— 后端应自行补上 adopted
    r = client.patch(f"/api/risks/{rid}", json={"suggestion_edited": "改后的建议"})
    assert r.status_code == 200
    assert r.json()["adopted"] is True

    # 清空编辑版后，adopted 保留（不反向推断）
    r2 = client.patch(f"/api/risks/{rid}", json={"suggestion_edited": ""})
    assert r2.status_code == 200
    assert r2.json()["suggestion_edited"] is None


def test_annotation_requires_risk_item(client: TestClient) -> None:
    """阶段一不支持纯整体批注，缺 risk_item_id 应 400。"""
    r = client.post(
        "/api/risks/annotations",
        json={"author": "张三", "content": "整体意见"},
    )
    assert r.status_code == 400


# ==================== 任务 ====================

def test_task_progress_404_for_missing(client: TestClient) -> None:
    assert client.get("/api/tasks/99999999/progress").status_code == 404


def test_retry_rejects_non_blocked_task(client: TestClient, db) -> None:
    """只有 blocked 任务可重试（状态机约束）。"""
    c = Contract(
        title="重试用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"retry-case").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.PENDING.value)
    db.add(t)
    db.commit()
    assert client.post(f"/api/tasks/{t.id}/retry").status_code == 409


def test_sync_todos_requires_approval_service(client: TestClient) -> None:
    """审批系统不可达时应 502，而不是 500 或静默返回空。"""
    if _approval_available():
        pytest.skip("mock 审批服务在运行，无法验证不可达路径")
    r = client.post("/api/tasks/sync-todos")
    assert r.status_code == 502


# ==================== 报告 / 回写前置条件 ====================

def test_report_preview_404_for_missing_contract(client: TestClient) -> None:
    assert client.get("/api/contracts/99999999/report/preview").status_code == 404


def test_writeback_status_404_for_missing_contract(client: TestClient) -> None:
    assert client.get("/api/contracts/99999999/writeback/status").status_code == 404


# ==================== 大盘筛选 / 批量操作（批次 6） ====================

def test_list_contracts_filters_by_risk_level(client: TestClient, db) -> None:
    """`risk_level` 筛选必须真的生效。

    回归点：该参数曾写作 `risk`，而前端与 api-guide 都用 `risk_level`，
    结果筛选被静默忽略、恒返回全量——用户看到的"筛选无效"即源于此。
    """
    c = Contract(
        title="筛选用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"filter-case").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.COMPLETED.value,
                   overall_risk="high")
    db.add(t)
    db.commit()

    assert client.get("/api/contracts", params={"risk_level": "high"}).json()["total"] == 1
    assert client.get("/api/contracts", params={"risk_level": "low"}).json()["total"] == 0
    # 已废弃的旧参数名不再接受，应退化为"不筛选"（返回全量）而非报错
    assert client.get("/api/contracts", params={"risk": "low"}).json()["total"] == 1


def test_batch_delete_reports_per_item(client: TestClient, db) -> None:
    """批量删除逐条回报：存在的删掉、不存在的说明原因，不整批回滚。"""
    c = Contract(
        title="批量删除用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"batch-del").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.commit()

    r = client.post("/api/contracts/batch/delete", json={"ids": [c.id, 99999999]})
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["succeeded"], body["failed"]) == (2, 1, 1)
    by_id = {item["id"]: item for item in body["results"]}
    assert by_id[c.id]["ok"] is True
    assert by_id[99999999]["ok"] is False
    assert client.get(f"/api/contracts/{c.id}").status_code == 404


def test_batch_retry_skips_non_blocked(client: TestClient, db) -> None:
    """批量重试只认 blocked；其余条目逐条回报失败原因。"""
    c = Contract(
        title="批量重试用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"batch-retry").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    t = ReviewTask(contract_id=c.id, status=TaskStatus.COMPLETED.value)
    db.add(t)
    db.commit()

    r = client.post("/api/tasks/batch/retry", json={"ids": [c.id, 99999999]})
    assert r.status_code == 200
    body = r.json()
    assert (body["succeeded"], body["failed"]) == (0, 2)
    details = " ".join(item["detail"] or "" for item in body["results"])
    assert "非阻塞状态" in details and "任务不存在" in details


def test_batch_rejects_empty_ids(client: TestClient) -> None:
    """空 ids 应被 schema 拦下（422），而不是返回空成功。"""
    assert client.post("/api/contracts/batch/delete", json={"ids": []}).status_code == 422


def test_list_export_records(client: TestClient, db) -> None:
    """导出记录列表返回可直接下载的 URL。"""
    c = Contract(
        title="导出记录用例", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"export-list").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    from app.models import ExportRecord
    db.add(ExportRecord(
        contract_id=c.id, format="markdown", file_object_key=f"{c.id}/r.md",
        file_size=123, created_by="法务",
    ))
    db.commit()

    r = client.get(f"/api/contracts/{c.id}/report/exports")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["format"] == "markdown"
    assert rows[0]["download_url"] == f"/api/contracts/{c.id}/report/download/{rows[0]['id']}"


def test_export_report_pdf(client: TestClient, db) -> None:
    """PDF 导出端到端：渲染 → 上传 MinIO → 下载回读。

    覆盖"精排"链路的关键契约：产物是可解析的 PDF，且 PRD 2.4.8 的四大章节
    都在正文里。**不走上传链路**——那样要等 WPS 转换 + 后台流水线跑完，
    慢且依赖外部环境；直接构造"已完成审查"的合同即可覆盖导出这一段。
    """
    import pymupdf

    from app.models import ReviewTask
    from app.models.enums import ReviewConclusion

    c = Contract(
        title="PDF 导出用例", business_type="purchase",
        file_format=FileFormat.PDF.value, file_object_key="x/original.pdf",
        file_name="x.pdf", file_size=10, contract_no="PDF-2026-0001",
        amount=100000, currency="CNY",
        file_hash=hashlib.sha256(b"pdf-export-case").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.flush()
    db.add(ReviewTask(
        contract_id=c.id, status=TaskStatus.COMPLETED.value,
        overall_risk="low", conclusion=ReviewConclusion.PASS.value,
        high_risk_count=0, medium_risk_count=0, low_risk_count=0,
        total_pages=1, parsed_pages=1,
    ))
    db.commit()

    r = client.post(f"/api/contracts/{c.id}/report/export", params={"format": "pdf"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["format"] == "pdf"
    assert body["file_size"] > 0
    assert body["object_key"].endswith(".pdf")

    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("application/pdf")

    doc = pymupdf.open(stream=dl.content, filetype="pdf")
    try:
        assert doc.page_count >= 1
        text = "".join(doc[i].get_text() for i in range(doc.page_count))
        for section in ("合同基本信息", "综合审查结论", "风险清单明细", "附录"):
            assert section in text, f"缺少章节: {section}"
        # PDF 文本层会在中英文之间插入 \xa0（不换行空格），比对前先归一化空白
        assert c.title.replace(" ", "") in text.replace("\xa0", "").replace(" ", "")
    finally:
        doc.close()


def test_export_report_rejects_unknown_format(client: TestClient) -> None:
    """非法格式应被 422 拦下，而不是静默按 Markdown 导出。"""
    r = client.post(
        "/api/contracts/99999999/report/export", params={"format": "docx"}
    )
    assert r.status_code == 422


def test_events_empty_for_contract_without_task(client: TestClient, db) -> None:
    c = Contract(
        title="无任务合同", business_type="purchase", file_format=FileFormat.PDF.value,
        file_object_key="x/original.pdf", file_name="x.pdf", file_size=10,
        file_hash=hashlib.sha256(b"no-task").hexdigest(),
        source=ContractSource.UPLOAD.value,
    )
    db.add(c)
    db.commit()
    r = client.get(f"/api/contracts/{c.id}/events")
    assert r.status_code == 200
    assert r.json() == []
