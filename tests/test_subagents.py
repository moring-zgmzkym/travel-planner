"""阶段三回归：JobBoard 任务体的 subagent 执行器（orchestrator→workers）。

- 工具调用产出从 ToolCallExecutionEvent 确定性提取（不解析自然语言）；
- 未调工具/产出非 dict → 重试 → 降级直调（上层 mock 兜底链不变）；
- 全部用 ReplayChatCompletionClient 桩，不联网。"""

import asyncio
import json

from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from tripmate.subagents import run_channel_subagent

_USAGE = RequestUsage(prompt_tokens=10, completion_tokens=10)


def _tool_call_client(n: int = 2):
    """n 次都返回"调用 _tool 工具"的模型响应。"""
    return ReplayChatCompletionClient([
        CreateResult(finish_reason="function_calls",
                     content=[FunctionCall(id=str(i), name="_tool", arguments="{}")],
                     usage=_USAGE, cached=False)
        for i in range(n)])


def _text_client(n: int = 2):
    """n 次都只回文本（不调工具）→ 触发降级直调。"""
    return ReplayChatCompletionClient(["我不调工具。"] * n)


def test_subagent_extracts_tool_output():
    async def main():
        async def query():
            return {"mode": "mock", "candidates": [1, 2]}

        out = await run_channel_subagent("tickets", "任务", query,
                                         model_client=_tool_call_client())
        assert out == {"mode": "mock", "candidates": [1, 2]}

    asyncio.run(main())


def test_subagent_degrades_to_direct_call_when_no_tool_invoked():
    """模型只回文本（不调工具）→ 重试后降级为直调查询函数，结果语义不变。"""
    async def main():
        async def query():
            return {"mode": "mock", "notice": "直调"}

        out = await run_channel_subagent("weather", "任务", query,
                                         model_client=_text_client(2))
        assert out == {"mode": "mock", "notice": "直调"}

    asyncio.run(main())


def test_subagent_degrades_when_tool_errors():
    """工具内部抛错（如 ServiceUnavailable）→ autogen 把异常转为错误文本 →
    提取失败 → 降级直调 → 直调同样抛错 → 上抛（进上层逐通道降级/mock）。"""
    from tripmate.tools.resilience import ServiceUnavailable

    async def main():
        async def query():
            raise ServiceUnavailable("外部服务不可用")

        try:
            await run_channel_subagent("hotels", "任务", query,
                                       model_client=_tool_call_client())
            raised = False
        except ServiceUnavailable:
            raised = True
        assert raised

    asyncio.run(main())


def test_subagent_tool_returns_json_string_payload():
    """工具产出经 JSON 序列化传递，提取端还原 dict（keys/中文原样）。"""
    async def main():
        async def query():
            return {"city": "成都&宽窄", "n": 2}

        out = await run_channel_subagent("guides", "任务", query,
                                         model_client=_tool_call_client())
        assert out == {"city": "成都&宽窄", "n": 2}

    asyncio.run(main())
