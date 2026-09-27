"""mock 审批服务：扮演外部审批系统，供后端联调。

⚠️ **独立服务**，与 backend 不共享代码（architecture.md §17）。
仅通过 HTTP 契约耦合，将来换真实审批系统只改后端的适配层。

四个能力（architecture.md §3 的 M1~M4）：
    M1 待办拉取   GET  /api/todos
    M2 附件下载   GET  /api/todos/{approval_no}/attachments/{attachment_id}
    M3 评论写入   POST /api/todos/{approval_no}/comments
    M4 事件推送   POST /api/events/push   （把事件 POST 回后端回调地址）

**为什么用标准库 http 客户端做事件推送**：本服务是桩，不值得为它引 httpx
依赖；后端那边才有 HTTP 客户端需求。用 `urllib` 足够。

启动：
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
（工作目录为 mock-approval/）
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from app.schemas import (
    Comment,
    CommentIn,
    CommentOut,
    EventPushOut,
    TodoItem,
)
from app.store import ATTACHMENT_DIR, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mock-approval")

#: 版本号与主项目保持一致（主项目版本源：backend/app/__init__.py）。
#: 本服务是独立桩服务，不共享 backend 代码，故此处独立维护、手动对齐。
MOCK_VERSION = "0.4.0"

app = FastAPI(
    title="Mock 审批系统",
    version=MOCK_VERSION,
    description="扮演外部审批系统：待办拉取 / 附件下载 / 评论写入 / 事件推送",
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """健康检查。后端启动自检会调它。"""
    return {
        "status": "ok",
        "service": "mock-approval",
        "todos": len(store.list_todos()),
    }


# ==================== M1 待办拉取 ====================

@app.get("/api/todos", response_model=list[TodoItem], tags=["M1 待办"])
def list_todos(
    status: str | None = Query(default=None, description="按状态过滤：pending / approved / rejected"),
) -> list[TodoItem]:
    """拉取待办审批单列表。"""
    return store.list_todos(status)


@app.get("/api/todos/{approval_no}", response_model=TodoItem, tags=["M1 待办"])
def get_todo(approval_no: str) -> TodoItem:
    """取单个审批单详情。"""
    todo = store.get_todo(approval_no)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {approval_no}")
    return todo


@app.post("/api/todos/{approval_no}/status", response_model=TodoItem, tags=["M1 待办"])
def update_status(approval_no: str, status: str = Query(description="新状态")) -> TodoItem:
    """更新审批单状态（模拟审批人操作，用于演示"审批通过/驳回"）。"""
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail=f"非法状态: {status}")
    todo = store.set_status(approval_no, status)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {approval_no}")
    return todo


# ==================== M2 附件下载 ====================

@app.get("/api/todos/{approval_no}/attachments", tags=["M2 附件"])
def list_attachments(approval_no: str) -> list[dict]:
    """列出审批单的附件（含下载地址）。"""
    todo = store.get_todo(approval_no)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {approval_no}")
    return [a.model_dump() for a in todo.attachments]


@app.get("/api/todos/{approval_no}/attachments/{attachment_id}", tags=["M2 附件"])
def download_attachment(approval_no: str, attachment_id: str) -> FileResponse:
    """下载附件字节流。"""
    path = store.attachment_path(approval_no, attachment_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"附件不存在: {approval_no} / {attachment_id}",
        )
    todo = store.get_todo(approval_no)
    att = next((a for a in todo.attachments if a.attachment_id == attachment_id), None)
    return FileResponse(
        path,
        filename=att.file_name if att else path.name,
        media_type=att.content_type if att else "application/octet-stream",
    )


# ==================== M3 评论写入 ====================

@app.post("/api/todos/{approval_no}/comments", response_model=CommentOut, tags=["M3 评论"])
def add_comment(approval_no: str, body: CommentIn) -> CommentOut:
    """写入审查意见评论。

    `idempotency_key` 命中时返回已有记录并置 `deduplicated=true`，
    供后端验证回写幂等。
    """
    if store.get_todo(approval_no) is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {approval_no}")
    comment, dedup = store.add_comment(
        approval_no, body.author, body.content, body.idempotency_key
    )
    return CommentOut(
        comment_id=comment.comment_id,
        approval_no=comment.approval_no,
        created_at=comment.created_at,
        deduplicated=dedup,
    )


@app.get("/api/todos/{approval_no}/comments", response_model=list[Comment], tags=["M3 评论"])
def list_comments(approval_no: str) -> list[Comment]:
    """查看审批单下的评论（演示时用来确认回写结果）。"""
    return store.list_comments(approval_no)


# ==================== M4 事件推送 ====================

@app.post("/api/events/push", response_model=EventPushOut, tags=["M4 事件"])
def push_event(
    approval_no: str = Query(description="要推送的审批单号"),
    target_url: str = Query(description="后端回调地址，如 http://127.0.0.1:8000/api/events/approval"),
) -> EventPushOut:
    """把审批事件推送到后端。

    真实审批系统是**主动推送**事件的，本接口复现这个方向：
    由 mock 发起 HTTP 请求，后端被动接收。

    推送失败**不抛 500**：真实场景下推送失败是常态（对方服务未起），
    调用方（演示脚本）需要看到 `delivered=false` 而不是异常。
    """
    todo = store.get_todo(approval_no)
    if todo is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {approval_no}")

    payload = json.dumps(
        {
            "event": "approval.submitted",
            "approval_no": todo.approval_no,
            "title": todo.title,
            "business_type": todo.business_type,
            "applicant": todo.applicant,
            "attachments": [a.model_dump() for a in todo.attachments],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        target_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
            logger.info("事件推送成功: %s -> %s", approval_no, target_url)
            return EventPushOut(
                approval_no=approval_no, target_url=target_url,
                http_status=resp.status, delivered=True, detail=body[:200],
            )
    except urllib.error.HTTPError as exc:
        logger.warning("事件推送被拒: %s %s", exc.code, exc.reason)
        return EventPushOut(
            approval_no=approval_no, target_url=target_url,
            http_status=exc.code, delivered=False, detail=f"HTTP {exc.code}: {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001 - 推送失败不抛 500，如实返回
        logger.warning("事件推送失败: %s", exc)
        return EventPushOut(
            approval_no=approval_no, target_url=target_url,
            http_status=None, delivered=False, detail=f"{type(exc).__name__}: {exc}",
        )


@app.get("/api/meta/attachments", tags=["meta"])
def list_attachment_files() -> dict:
    """列出附件目录里的物理文件，便于排查"数据文件与实际附件不匹配"。"""
    if not ATTACHMENT_DIR.exists():
        return {"dir": str(ATTACHMENT_DIR), "files": []}
    return {
        "dir": str(ATTACHMENT_DIR),
        "files": sorted(p.name for p in ATTACHMENT_DIR.iterdir() if p.is_file()),
    }
