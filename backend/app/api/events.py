"""事件接收路由：审批系统推送的入口。

设计依据：docs/architecture.md §11.1（审批触发流程）。

**方向是反的**：真实审批系统主动推送事件到本系统，所以这里是
"被调用方"。mock 服务的 `/api/events/push` 就是往这里 POST。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])


@router.post("/approval", summary="接收审批系统事件推送")
async def receive_approval_event(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """接收审批事件。

    **只记录与应答，不同步拉取**：拉取动作由 `/api/tasks/sync-todos` 显式触发。
    这样"事件到达"与"真的去拉数据"解耦，演示时更容易讲清流程，
    也避免推送方等待长时间的处理（真实系统的推送通常有超时限制）。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON 不应 500
        logger.warning("收到无法解析的审批事件")
        return {"ok": False, "detail": "请求体不是合法 JSON"}

    logger.info(
        "收到审批事件: event=%s approval_no=%s title=%s",
        payload.get("event"), payload.get("approval_no"), payload.get("title"),
    )
    return {
        "ok": True,
        "received": {
            "event": payload.get("event"),
            "approval_no": payload.get("approval_no"),
        },
        "hint": "调用 POST /api/tasks/sync-todos 拉取待办并启动审查",
    }
