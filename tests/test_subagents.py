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
    """n 次都返回"调用 query_channel_data 工具"的模型响应。"""
    return ReplayChatCompletionClient([
        CreateResult(finish_reason="function_calls",
                     content=[FunctionCall(id=str(i), name="query_channel_data", arguments="{}")],
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


def test_subagent_notify_sequence_success():
    """指示灯状态序列（成功路径）：running → done。"""
    import asyncio
    states = []

    async def notify(state):
        states.append(state)

    async def main():
        async def query():
            return {"ok": 1}

        out = await run_channel_subagent("tickets", "任务", query,
                                         model_client=_tool_call_client(), notify=notify)
        assert out == {"ok": 1}

    asyncio.run(main())
    assert states == ["running", "done"]


def test_subagent_notify_degrade_then_done():
    """降级直调成功也发 done（前端灯照样变绿），序列 running → done。"""
    import asyncio
    states = []

    async def notify(state):
        states.append(state)

    async def main():
        async def query():
            return {"mode": "mock"}

        out = await run_channel_subagent("weather", "任务", query,
                                         model_client=_text_client(2), notify=notify)
        assert out == {"mode": "mock"}

    asyncio.run(main())
    assert states == ["running", "done"]


def test_subagent_notify_failed_and_reraises():
    """subagent 未产出 + 直调也失败：failed 后原异常透传（上层逐通道降级）。"""
    import asyncio
    from tripmate.tools.resilience import ServiceUnavailable
    states = []

    async def notify(state):
        states.append(state)

    async def main():
        async def query():
            raise ServiceUnavailable("外部服务不可用")

        try:
            await run_channel_subagent("hotels", "任务", query,
                                       model_client=_text_client(2), notify=notify)
            raised = False
        except ServiceUnavailable:
            raised = True
        assert raised

    asyncio.run(main())
    assert states == ["running", "failed"]


def test_subagent_notify_errors_never_break_query():
    """回调自身抛异常：被吞掉并记日志，查询结果照常返回（绝不触发重试/降级）。"""
    import asyncio
    calls = {"n": 0}

    async def notify(state):
        calls["n"] += 1
        raise RuntimeError("推送通道炸了")

    async def main():
        async def query():
            return {"ok": True}

        out = await run_channel_subagent("guides", "任务", query,
                                         model_client=_tool_call_client(), notify=notify)
        assert out == {"ok": True}

    asyncio.run(main())
    assert calls["n"] == 2  # running + done 都尝试推送了


def test_route_channel_extracts_and_degrades():
    """route 通道与既有通道同协议：工具产出确定性提取 + 未调工具降级直调。"""
    async def main():
        async def query():
            return {"days": [{"date": "2026-10-01", "stops": [], "segments": []}],
                    "notice": None}

        out = await run_channel_subagent("route", "任务", query,
                                         model_client=_tool_call_client())
        assert out["days"] and out["days"][0]["date"] == "2026-10-01"
        out2 = await run_channel_subagent("route", "任务", query,
                                          model_client=_text_client(2))
        assert out2["notice"] is None

    asyncio.run(main())
