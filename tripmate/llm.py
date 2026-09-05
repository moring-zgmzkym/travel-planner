"""共享模型客户端工厂（§3.7：OpenAI 兼容接口，全部 Agent 经此调用）+ token 成本控制（§2.3）。

五个 Agent 共享同一个客户端实例：total_usage() 聚合全局消耗，支撑 500K 单次规划上限。
配置次级模型（LLM_FALLBACK_* 三变量齐备）时返回主备自动切换的包装客户端：
主模型失败（网络/限流/超时）即切次级，冷却期过后自动重探主模型、恢复即切回。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Literal, Mapping, Optional, Sequence

import openai
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

# 连接类错误（APIConnectionError）的同通道快速重试延迟（秒）。
# 实测（2026-09-04 09:52）：主备两通道同轮各报一次 APIConnectionError（瞬时网络/VPN 抖动），
# 用户直接收到失败提示。连接错误秒级失败，同通道快速重试一次大概率救回；
# 超时不重试（已等满 150/300s，再等翻倍）、429 不重试（限流窗口以分钟计）——二者维持直切次级。
_CONN_RETRY_DELAY_S = 3.0


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
            tries = 0
            while True:
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
                    tries += 1
                    # 注意 APITimeoutError 是 APIConnectionError 的子类：超时已等满超时上限，
                    # 不得快速重试（翻倍等待），仅纯连接错误（秒级失败）享受快速重试
                    if (isinstance(exc, openai.APIConnectionError)
                            and not isinstance(exc, openai.APITimeoutError) and tries < 2):
                        logger.warning("%s连接错误（%s: %s），%.0fs 后同通道快速重试",
                                       self._label(idx), type(exc).__name__, exc, _CONN_RETRY_DELAY_S)
                        await asyncio.sleep(_CONN_RETRY_DELAY_S)
                        continue
                    last_exc = exc
                    self._note_failure(idx, exc)
                    break
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
                tries = 0
                while True:
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
                        if yielded:
                            # 流中途失败无法安全切换（避免重复输出半截结果），原样上抛
                            raise
                        tries += 1
                        # APITimeoutError 是 APIConnectionError 子类，同样排除（不快速重试超时）
                        if (isinstance(exc, openai.APIConnectionError)
                                and not isinstance(exc, openai.APITimeoutError) and tries < 2):
                            logger.warning("%s连接错误（%s: %s），%.0fs 后同通道快速重试",
                                           self._label(idx), type(exc).__name__, exc,
                                           _CONN_RETRY_DELAY_S)
                            await asyncio.sleep(_CONN_RETRY_DELAY_S)
                            continue
                        last_exc = exc
                        self._note_failure(idx, exc)
                        break
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


# 单次规划计数基线（2026-08-31）：熔断语义为"单次完整规划"（§2.3），而底层客户端计数器
# 进程级累计且 OpenAIChatCompletionClient 未实现 reset_usage（实测 AttributeError）——
# 以"起点快照 + 差值"实现按次记账：TeamRunner.start() 调 reset_usage() 重新锚定。
# 2026-09-05：模块级基线是进程级单例，多会话并发时互相重锚/互相熔断——TeamRunner 改持
# 每 run 基线并显式传入 check_budget；模块级基线仅保留给 usage_summary 展示与兼容。
_usage_baseline = RequestUsage(prompt_tokens=0, completion_tokens=0)


def reset_usage() -> None:
    """重新锚定模块级计数基线（TeamRunner.start 时调用，兼容入口）。"""
    global _usage_baseline
    _usage_baseline = snapshot_usage()


def snapshot_usage() -> RequestUsage:
    """当前进程累计消耗快照（per-run 基线取这里）。"""
    return _client.total_usage() if _client is not None else RequestUsage(
        prompt_tokens=0, completion_tokens=0)


def _run_usage(baseline: RequestUsage | None = None) -> RequestUsage:
    """本次规划（自基线起）的消耗 = 进程累计 − 基线。baseline 为空回退模块级基线。"""
    base = baseline if baseline is not None else _usage_baseline
    u = snapshot_usage()
    return RequestUsage(
        prompt_tokens=max(0, u.prompt_tokens - base.prompt_tokens),
        completion_tokens=max(0, u.completion_tokens - base.completion_tokens),
    )


def check_budget(baseline: RequestUsage | None = None) -> None:
    """熔断检查：baseline 缺省回退模块级基线；TeamRunner 传每 run 基线实现多会话隔离。"""
    u = _run_usage(baseline)
    used = u.prompt_tokens + u.completion_tokens
    if used > BudgetConfig.TOKEN_LIMIT:
        raise TokenBudgetExceeded(
            f"token 消耗 {used} 已超上限 {BudgetConfig.TOKEN_LIMIT}，规划终止（§2.3 成本控制）。"
        )


def usage_summary(baseline: RequestUsage | None = None) -> dict:
    if _client is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "limit": BudgetConfig.TOKEN_LIMIT}
    u = _run_usage(baseline)
    return {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "total_tokens": u.prompt_tokens + u.completion_tokens,
        "limit": BudgetConfig.TOKEN_LIMIT,
    }
