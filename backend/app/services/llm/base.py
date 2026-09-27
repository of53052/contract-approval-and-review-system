"""LLM Provider 抽象。

设计依据：docs/architecture.md §9.1（接口设计）、§9.2（只用两个实现）。

只定义**最小必要接口**：`chat` 与 `chat_json`。
- `chat` 返回纯文本，供自由生成（如摘要）
- `chat_json` 返回已校验的结构化结果，**JSON 兜底逻辑对调用方透明**

不引入额外 SDK：`OpenAICompatProvider` 用 `openai` 库的自定义 `base_url`，
一套代码兼容 DeepSeek / 通义 / 智谱 / Kimi / 自建 vLLM / Ollama。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ChatMessage:
    """一条对话消息。"""

    role: str  # system / user / assistant
    content: str


@dataclass
class LlmUsage:
    """token 用量，用于 llm_call_log 审计与成本观测。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class ChatResult:
    """一次调用的完整结果。"""

    content: str
    usage: LlmUsage = field(default_factory=LlmUsage)
    #: JSON 解析重试次数（仅 chat_json 有意义），落库到 llm_call_log
    json_retry_count: int = 0
    #: 原始响应片段，供审计（落库到 risk_evidence.raw_snippet）
    raw: str | None = None
    #: 模型返回的推理链（部分推理模型会单独返回，**必须与 content 区分**）
    reasoning_content: str | None = None


class LlmError(Exception):
    """LLM 调用失败（网络、鉴权、超时等）。不可恢复，向上抛。"""


class LlmJsonError(LlmError):
    """JSON 兜底全部失败。对应 `llm_call_log.status = 'invalid_json'`。"""

    def __init__(self, message: str, *, raw: str | None = None,
                 retry_count: int = 0) -> None:
        super().__init__(message)
        self.raw = raw
        self.retry_count = retry_count


@runtime_checkable
class LLMProvider(Protocol):
    """Provider 接口。两个实现：MockProvider / OpenAICompatProvider。"""

    name: str

    def is_available(self) -> bool: ...

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult: ...

    def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        validator: Any = None,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> tuple[Any, ChatResult]: ...
