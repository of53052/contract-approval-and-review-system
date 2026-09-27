"""Redis 客户端与缓存键规划。

缓存键命名与 TTL 见 docs/data-model.md §7.1 / §8.2。
"""

from __future__ import annotations

import redis

from app.core.config import settings

# 连接池复用，避免每次请求新建连接
_pool = redis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,  # 直接返回 str，避免各处手动 decode
    max_connections=20,
)


def get_redis() -> redis.Redis:
    """获取 Redis 客户端（连接池复用）。"""
    return redis.Redis(connection_pool=_pool)


# ==================== 缓存键规划 ====================
# 集中定义避免键名散落各处拼错。TTL 单位：秒。

TTL_TASK_PROGRESS = 24 * 3600      # 解析进度：1 天
TTL_OCR_CACHE = 7 * 24 * 3600      # OCR 结果：7 天
TTL_ANCHOR_CACHE = 7 * 24 * 3600   # 锚点对齐：7 天
TTL_LLM_CACHE = 3600               # LLM 响应：1 小时


def key_task_progress(task_id: int) -> str:
    """解析进度键。高频写入，任务完成后清理。"""
    return f"task:{task_id}:progress"


#: OCR 缓存键的版本号。**改变坐标算法时必须 +1**。
#: 缓存的是 `OcrPageResult` 整体（含 `char_boxes`），换算法后旧条目里的坐标
#: 已经不对，但键名不变则仍会被命中——表现为"改了代码却没效果"。
#: v1 → v2：字符框由"行内等宽切分"改为 OCR 的字符级框（见 R14'）。
OCR_CACHE_VERSION = 2


def key_ocr_cache(file_hash: str, page_no: int) -> str:
    """OCR 结果缓存键。

    用 file_hash 而非 contract_id：同一份文件重复上传时可直接命中，
    避免重跑 6 秒/页的 OCR（见 docs/data-model.md §8.2）。

    键里带 `OCR_CACHE_VERSION`：换坐标算法后旧键自然失配，
    无需手工清理 Redis（见该常量的说明）。
    """
    return f"ocr:v{OCR_CACHE_VERSION}:{file_hash}:{page_no}"


def key_anchor_cache(contract_id: int, quote_hash: str) -> str:
    """锚点对齐缓存键。避免重复做模糊匹配。"""
    return f"anchor:{contract_id}:{quote_hash}"


def key_llm_cache(prompt_hash: str) -> str:
    """LLM 响应缓存键。演示期避免重复调用。"""
    return f"llm:{prompt_hash}"


def check_connection() -> tuple[bool, str]:
    """连通性自检。返回 (是否成功, 详情)。"""
    try:
        client = get_redis()
        info = client.info("server")
        return True, f"Redis {info.get('redis_version')} | keys={client.dbsize()}"
    except Exception as exc:  # noqa: BLE001 - 自检需捕获全部异常
        return False, f"{type(exc).__name__}: {exc}"
