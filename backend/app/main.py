"""FastAPI 应用入口。

设计依据：docs/architecture.md §4（部署架构）、§12（模块划分）。

**原生运行**（不容器化）：DOCX 分页依赖 WPS COM，需要交互式桌面会话。
启动：
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
（工作目录 backend/）
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时做一次基础设施自检。

    **不因自检失败而拒绝启动**：阶段一是演示系统，某个依赖没起时
    应该能打开 /docs 看到错误提示，而不是进程直接退出。
    """
    logger.info("=" * 60)
    logger.info("合同审查系统后端启动中（env=%s）", settings.app_env)
    for name, fn in (
        ("MySQL", _check_mysql),
        ("Redis", _check_redis),
        ("MinIO", _check_minio),
        ("审批系统", _check_approval),
    ):
        try:
            ok, detail = fn()
            logger.info("  %-8s %s | %s", name, "✓" if ok else "✗", detail)
        except Exception as exc:  # noqa: BLE001 - 自检失败不阻断启动
            logger.warning("  %-8s ✗ | 自检异常: %s", name, exc)
    logger.info("=" * 60)
    yield
    logger.info("合同审查系统后端已停止")


def _check_mysql() -> tuple[bool, str]:
    from app.core.database import check_connection
    return check_connection()


def _check_redis() -> tuple[bool, str]:
    from app.core.redis_client import get_redis
    return True, f"Redis 可达 | ping={get_redis().ping()}"


def _check_minio() -> tuple[bool, str]:
    from app.core.minio_client import check_connection
    return check_connection()


def _check_approval() -> tuple[bool, str]:
    from app.services.approval import get_adapter
    return get_adapter().health()


app = FastAPI(
    title="合同审批审查系统",
    version="0.4.0",
    description="智能合同审查：解析 → 条款切分 → 双引擎审查 → 高亮定位 → 回写审批系统",
    lifespan=lifespan,
)

# CORS：前端 Vite dev server 独立端口，必须放行。
# 阶段一是本地开发环境，允许 localhost 全端口即可。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    """服务健康检查。"""
    return {"status": "ok", "service": "contract-review-backend", "version": "0.4.0"}


@app.get("/api/meta/config", tags=["meta"], summary="前端可见的运行时配置")
def runtime_config() -> dict:
    """暴露给前端的非敏感配置。

    ⚠️ **只暴露展示相关项**，绝不返回任何凭据（架构文档 §15.1）。
    """
    return {
        "app_env": settings.app_env,
        "llm_provider": "openai_compat" if settings.use_real_llm else "mock",
        "docx_converter": settings.docx_converter,
        "minio_bucket_contracts": settings.minio_bucket_contracts,
    }
