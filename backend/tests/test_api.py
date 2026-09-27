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

from app.core.database import SessionLocal
from app.main import app
from app.models import Contract, ReviewTask, RiskItem
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
