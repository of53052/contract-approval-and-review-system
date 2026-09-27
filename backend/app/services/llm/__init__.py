"""LLM Provider 工厂与导出。

调用方统一用 `get_provider()` 获取 Provider，**不直接实例化具体类**，
这样"无 Key 自动回落 Mock"的逻辑只有一处实现。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.services.llm.base import (
    ChatMessage,
    ChatResult,
    LLMProvider,
    LlmError,
    LlmJsonError,
    LlmUsage,
)
from app.services.llm.json_utils import JsonExtractionError, extract_json
from app.services.llm.mock_provider import MockProvider
from app.services.llm.openai_compat import OpenAICompatProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ChatMessage",
    "ChatResult",
    "LLMProvider",
    "LlmError",
    "LlmJsonError",
    "LlmUsage",
    "JsonExtractionError",
    "extract_json",
    "MockProvider",
    "OpenAICompatProvider",
    "get_provider",
]


def get_provider(*, force_mock: bool = False) -> LLMProvider:
    """获取 Provider。

    回落规则（见 docs/architecture.md §9.3）：
    - `LLM_PROVIDER=mock` 或配置不完整 → MockProvider
    - `LLM_PROVIDER=openai_compat` 且三项齐备 → OpenAICompatProvider

    **默认 mock**：无 Key 时演示仍能完整跑通全链路。
    """
    if force_mock:
        logger.info("强制使用 MockProvider")
        return MockProvider()

    if settings.use_real_llm:
        provider = OpenAICompatProvider()
        if provider.is_available():
            logger.info(
                "使用 OpenAICompatProvider: %s | model=%s",
                provider.base_url, provider.model,
            )
            return provider
        logger.warning("OpenAICompatProvider 配置不完整，回落到 MockProvider")

    logger.info("使用 MockProvider（provider=%s）", settings.llm_provider)
    return MockProvider()
