"""API 路由层。

**路由只做编排**：参数校验 → 调 service / workers → 转成响应模型。
业务逻辑不写在这里——它属于 `services/`。

阶段一的路由（architecture.md §12）：
    contracts / tasks / risks / rules / writeback / reports / events
"""

from fastapi import APIRouter

from app.api import contracts, events, reports, risks, rules, tasks, writeback

#: 汇总路由，供 main.py 一次性挂载
api_router = APIRouter()
api_router.include_router(contracts.router)
api_router.include_router(tasks.router)
api_router.include_router(risks.router)
api_router.include_router(rules.router)
api_router.include_router(writeback.router)
api_router.include_router(reports.router)
api_router.include_router(events.router)

__all__ = ["api_router"]
