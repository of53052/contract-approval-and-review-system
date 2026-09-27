"""OpenAI 兼容 Provider。

设计依据：docs/architecture.md §9.1、§9.2、§9.4。

用 `openai` 库的自定义 `base_url`，一套代码兼容 DeepSeek / 通义 / 智谱 /
Kimi / 硅基流动 / 自建 vLLM / Ollama。**不引入额外 SDK。**

### 实测发现的两个关键事实（本项目实际端点）

1. **`response_format={"type":"json_object"}` 被接受但无效**：
   实测 3/3 次返回 markdown 包裹 + 尾部解释文字。因此 `chat_json`
   必须走「提取 + 校验 + 重试」三层兜底，不能依赖该参数。

2. **响应含 `reasoning_content`**（推理模型的思维链），与 `content` 分开。
   必须正确忽略，否则会把思维链当成正文解析。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import settings
from app.services.llm.base import (
    ChatMessage,
    ChatResult,
    LlmError,
    LlmJsonError,
    LlmUsage,
)
from app.services.llm.json_utils import JsonExtractionError, build_retry_hint, extract_json

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """基于 openai SDK 的通用 Provider。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url or "").rstrip("/")
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout
        self._client = None

    def is_available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def _get_client(self):
        """延迟创建客户端：避免无 Key 时导入即报错。"""
        if self._client is None:
            if not self.is_available():
                raise LlmError(
                    "OpenAICompatProvider 配置不完整，需要 LLM_BASE_URL / "
                    "LLM_API_KEY / LLM_MODEL 三者齐备"
                )
            from openai import OpenAI

            # openai 库要求 base_url 通常带 /v1；若用户给的是裸域名则补上
            base = self.base_url
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            self._client = OpenAI(api_key=self.api_key, base_url=base, timeout=self.timeout)
        return self._client

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        """单轮对话。

        不做重试——重试策略由上层（`chat_json` 或调用方）决定，
        因为"网络失败重试"与"JSON 格式错误重试"的语义不同。
        """
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        t0 = time.perf_counter()
        try:
            resp = client.chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - 统一转 LlmError，便于上层分类处理
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc
        duration_ms = int((time.perf_counter() - t0) * 1000)

        choice = resp.choices[0] if resp.choices else None
        if choice is None:
            raise LlmError("LLM 返回空的 choices")
        content = choice.message.content or ""

        # 推理模型会单独返回思维链，**必须与 content 区分**（实测发现）
        reasoning = getattr(choice.message, "reasoning_content", None)

        usage = LlmUsage()
        if resp.usage is not None:
            usage = LlmUsage(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
            )

        logger.info(
            "LLM 调用完成: model=%s 耗时 %s ms tokens=%s/%s 输出 %s 字符",
            self.model, duration_ms, usage.prompt_tokens, usage.completion_tokens,
            len(content),
        )
        return ChatResult(
            content=content,
            usage=usage,
            raw=content,
            reasoning_content=reasoning,
        )

    def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        validator: Any = None,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> tuple[Any, ChatResult]:
        """要求结构化输出，带 JSON 三层兜底。

        `validator` 是可选的可调用对象（通常传 Pydantic 模型的 `model_validate`），
        校验失败也触发重试——因为"能解析成 JSON 但字段不合法"同样不可用。
        """
        convo = list(messages)
        last_error: str | None = None
        last_raw: str | None = None
        total_usage = LlmUsage(prompt_tokens=0, completion_tokens=0)

        for attempt in range(max_retries + 1):
            # 第 0 次尝试带上 json_object（端点可能支持）；重试时不再带，
            # 因为实测该参数无效，继续带只会浪费一次机会
            resp_format = {"type": "json_object"} if attempt == 0 else None
            result = self.chat(
                convo, temperature=temperature, response_format=resp_format
            )
            last_raw = result.content
            total_usage.prompt_tokens = (total_usage.prompt_tokens or 0) + (
                result.usage.prompt_tokens or 0
            )
            total_usage.completion_tokens = (total_usage.completion_tokens or 0) + (
                result.usage.completion_tokens or 0
            )

            try:
                data = extract_json(result.content)
            except JsonExtractionError as exc:
                last_error = f"JSON 提取失败: {exc}"
                logger.warning("第 %s 次 JSON 提取失败，准备重试", attempt + 1)
                if attempt < max_retries:
                    convo = list(messages) + [
                        ChatMessage(role="assistant", content=result.content[:1000]),
                        ChatMessage(role="user",
                                    content=build_retry_hint(last_error, result.content)),
                    ]
                continue

            if validator is not None:
                try:
                    validated = validator(data)
                except Exception as exc:  # noqa: BLE001 - 校验失败也走重试
                    last_error = f"schema 校验失败: {exc}"
                    logger.warning("第 %s 次校验失败，准备重试: %s", attempt + 1, exc)
                    if attempt < max_retries:
                        convo = list(messages) + [
                            ChatMessage(role="assistant", content=result.content[:1000]),
                            ChatMessage(role="user",
                                        content=build_retry_hint(last_error, result.content)),
                        ]
                    continue
                return validated, ChatResult(
                    content=result.content,
                    usage=total_usage,
                    json_retry_count=attempt,
                    raw=last_raw,
                    reasoning_content=result.reasoning_content,
                )

            return data, ChatResult(
                content=result.content,
                usage=total_usage,
                json_retry_count=attempt,
                raw=last_raw,
                reasoning_content=result.reasoning_content,
            )

        raise LlmJsonError(
            f"JSON 兜底全部失败（重试 {max_retries} 次）: {last_error}",
            raw=last_raw,
            retry_count=max_retries,
        )
