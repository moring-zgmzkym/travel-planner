"""共享模型客户端工厂（§3.7：OpenAI 兼容接口，全部 Agent 经此调用）+ token 成本控制（§2.3）。

五个 Agent 共享同一个客户端实例：total_usage() 聚合全局消耗，支撑 200K 上限。
"""

from __future__ import annotations

from autogen_core.models import ModelInfo
from autogen_ext.models.openai import OpenAIChatCompletionClient

from .config import LLMConfig, BudgetConfig


class TokenBudgetExceeded(RuntimeError):
    """单次完整规划 token 消耗超上限（§2.3：终止并提示）。"""


_client: OpenAIChatCompletionClient | None = None


def get_model_client() -> OpenAIChatCompletionClient:
    """惰性单例。hy3-free 等非 OpenAI 官方模型名必须显式传 model_info。"""
    global _client
    if _client is None:
        if not LLMConfig.API_KEY:
            raise RuntimeError("未配置 LLM_API_KEY（.env），无法调用模型。")
        _client = OpenAIChatCompletionClient(
            model=LLMConfig.MODEL,
            api_key=LLMConfig.API_KEY,
            base_url=LLMConfig.BASE_URL,
            temperature=LLMConfig.TEMPERATURE,
            max_tokens=LLMConfig.MAX_TOKENS,
            # 预算从 .env 读取（默认 150s×2 次尝试，最坏 ~5 分钟/调用而非 20 分钟），
            # 失败尽快暴露给 Agent 自愈/护栏/降级链路
            max_retries=LLMConfig.MAX_RETRIES,
            timeout=LLMConfig.TIMEOUT_S,
            model_info=ModelInfo(
                vision=False,
                function_calling=True,
                json_output=False,
                structured_output=False,
                family="unknown",
            ),
        )
    return _client


def total_tokens() -> int:
    """本次进程累计 token 消耗（prompt + completion）。"""
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
