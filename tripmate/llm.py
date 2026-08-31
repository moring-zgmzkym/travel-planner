"""共享模型客户端工厂（§3.7：OpenAI 兼容接口，全部 Agent 经此调用）+ token 成本控制（§2.3）。

五个 Agent 共享同一个客户端实例：total_usage() 聚合全局消耗，支撑 200K 上限。
配置次级模型（LLM_FALLBACK_* 三变量齐备）时返回主备自动切换的包装客户端：
主模型失败（网络/限流/超时）即切次级，冷却期过后自动重探主模型、恢复即切回。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncGenerator, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel

from autogen_core import CancellationToken, Component
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    ModelCapabilities,  # type: ignore
    ModelInfo,
    RequestUsage,
)
from autogen_core.tools import Tool, ToolSchema
from autogen_ext.models.openai import OpenAIChatCompletionClient

from .config import BudgetConfig, LLMConfig

logger = logging.getLogger("tripmate.llm")


class TokenBudgetExceeded(RuntimeError):
    """单次完整规划 token 消耗超上限（§2.3：终止并提示）。"""


# hy3-free / glm-5.3-flash 等非 OpenAI 官方模型名必须显式传 model_info
_MODEL_INFO = ModelInfo(
    vision=False,
    function_calling=True,
    json_output=False,
    structured_output=False,
    family="unknown",
)

# 主模型失败后的冷却期（秒）：期内直接走次级，避免每次调用都白等一次超时；
# 冷却过后重新探测主模型，恢复即切回。
_PRIMARY_COOLDOWN_S = 120.0


def _build_client(base_url: str, api_key: str, model: str, timeout: float) -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=LLMConfig.TEMPERATURE,
        max_tokens=LLMConfig.MAX_TOKENS,
        # 主 150s / 备 300s（LLMConfig 注释：主备差异化实测依据）；失败尽快暴露给
        # Agent 自愈/护栏/降级链路，跨通道切换即有效重试（SDK 同通道重试已关闭）
        max_retries=LLMConfig.MAX_RETRIES,
        timeout=timeout,
        model_info=_MODEL_INFO,
    )


def _sum_usage(a: RequestUsage, b: RequestUsage) -> RequestUsage:
    return RequestUsage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
    )


class _FallbackConfig(BaseModel):
    """组件序列化配置（应用内不持久化客户端，仅为满足基类契约）。"""

    primary: Any
    secondary: Any


class _FallbackClient(ChatCompletionClient, Component[_FallbackConfig]):
    """主备双客户端包装：按 [主, 次] 顺序尝试，主失败粘性切次级 + 冷却期自动切回。

    total_usage()/actual_usage() 聚合主备两端（§2.3 成本熔断与前端用量推送依赖）。
    """

    component_type = "model"
    component_config_schema = _FallbackConfig

    def __init__(self, primary: ChatCompletionClient, secondary: ChatCompletionClient) -> None:
        self._primary = primary
        self._secondary = secondary
        self._on_primary = True
        self._primary_failed_at = 0.0

    def _label(self, idx: int) -> str:
        return "主模型" if idx == 0 else "次级模型"

    def _order(self) -> list[int]:
        if self._on_primary or time.monotonic() - self._primary_failed_at > _PRIMARY_COOLDOWN_S:
            return [0, 1]
        return [1, 0]

    def _client(self, idx: int) -> ChatCompletionClient:
        return self._primary if idx == 0 else self._secondary

    def _note_success(self, idx: int) -> None:
        if not self._on_primary and idx == 0:
            logger.info("主模型恢复，切回主模型")
        self._on_primary = idx == 0

    def _note_failure(self, idx: int, exc: Exception) -> None:
        if idx == 0:
            self._primary_failed_at = time.monotonic()
            self._on_primary = False
            logger.warning(
                "主模型调用失败（%s: %s），本次及 %ss 内改用次级模型重试",
                type(exc).__name__, exc, int(_PRIMARY_COOLDOWN_S),
            )

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> CreateResult:
        last_exc: Exception | None = None
        for idx in self._order():
            try:
                result = await self._client(idx).create(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    json_output=json_output,
                    extra_create_args=extra_create_args,
                    cancellation_token=cancellation_token,
                )
                self._note_success(idx)
                return result
            except Exception as exc:  # noqa: BLE001 — 主备逐个尝试，全部失败才上抛
                last_exc = exc
                self._note_failure(idx, exc)
        assert last_exc is not None
        raise last_exc

    def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: Optional[bool | type[BaseModel]] = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: Optional[CancellationToken] = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        async def _generator() -> AsyncGenerator[str | CreateResult, None]:
            last_exc: Exception | None = None
            for idx in self._order():
                yielded = False
                try:
                    async for chunk in self._client(idx).create_stream(
                        messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        json_output=json_output,
                        extra_create_args=extra_create_args,
                        cancellation_token=cancellation_token,
                    ):
                        yielded = True
                        yield chunk
                    self._note_success(idx)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if yielded:
                        # 流中途失败无法安全切换（避免重复输出半截结果），原样上抛
                        raise
                    self._note_failure(idx, exc)
            assert last_exc is not None
            raise last_exc

        return _generator()

    async def close(self) -> None:
        await self._primary.close()
        await self._secondary.close()

    def actual_usage(self) -> RequestUsage:
        return _sum_usage(self._primary.actual_usage(), self._secondary.actual_usage())

    def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        idx = self._order()[0]
        return self._client(idx).count_tokens(messages, tools=tools)

    @property
    def capabilities(self) -> ModelCapabilities:  # type: ignore
        return self._primary.capabilities

    @property
    def model_info(self) -> ModelInfo:
        return self._primary.model_info

    def remaining_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        idx = self._order()[0]
        return self._client(idx).remaining_tokens(messages, tools=tools)

    def total_usage(self) -> RequestUsage:
        return _sum_usage(self._primary.total_usage(), self._secondary.total_usage())

    def reset_usage(self) -> None:
        self._primary.reset_usage()
        self._secondary.reset_usage()

    def _to_config(self) -> _FallbackConfig:
        return _FallbackConfig(
            primary=self._primary.dump_component().model_dump(),
            secondary=self._secondary.dump_component().model_dump(),
        )

    @classmethod
    def _from_config(cls, config: _FallbackConfig) -> "_FallbackClient":
        return cls(
            primary=ChatCompletionClient.load_component(config.primary),
            secondary=ChatCompletionClient.load_component(config.secondary),
        )


_client: ChatCompletionClient | None = None


def get_model_client() -> ChatCompletionClient:
    """惰性单例。LLM_FALLBACK_* 三变量齐备时返回主备自动切换的包装客户端。"""
    global _client
    if _client is None:
        if not LLMConfig.API_KEY:
            raise RuntimeError("未配置 LLM_API_KEY（.env），无法调用模型。")
        primary = _build_client(LLMConfig.BASE_URL, LLMConfig.API_KEY, LLMConfig.MODEL,
                                timeout=LLMConfig.TIMEOUT_S)
        if LLMConfig.FALLBACK_API_KEY and LLMConfig.FALLBACK_BASE_URL and LLMConfig.FALLBACK_MODEL:
            secondary = _build_client(
                LLMConfig.FALLBACK_BASE_URL, LLMConfig.FALLBACK_API_KEY, LLMConfig.FALLBACK_MODEL,
                timeout=LLMConfig.FALLBACK_TIMEOUT_S)
            logger.info("模型客户端就绪：主 %s，次级 %s（自动故障切换已启用）",
                        LLMConfig.MODEL, LLMConfig.FALLBACK_MODEL)
            _client = _FallbackClient(primary, secondary)
        else:
            logger.info("模型客户端就绪：%s（未配置次级模型，无自动切换）", LLMConfig.MODEL)
            _client = primary
    return _client


def total_tokens() -> int:
    """本次进程累计 token 消耗（prompt + completion，主备聚合）。"""
    if _client is None:
        return 0
    usage = _client.total_usage()
    return usage.prompt_tokens + usage.completion_tokens


def check_budget() -> None:
    if total_tokens() > BudgetConfig.TOKEN_LIMIT:
        raise TokenBudgetExceeded(
            f"token 消耗 {total_tokens()} 已超上限 {BudgetConfig.TOKEN_LIMIT}，规划终止（§2.3 成本控制）。"
        )


def usage_summary() -> dict:
    if _client is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "limit": BudgetConfig.TOKEN_LIMIT}
    u = _client.total_usage()
    return {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.prompt_tokens + u.completion_tokens,
        "limit": BudgetConfig.TOKEN_LIMIT,
    }
