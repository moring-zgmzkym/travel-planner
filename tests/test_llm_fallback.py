"""主备模型自动切换（llm._FallbackClient）单测：stub 客户端，不联网。

覆盖：主正常不切换 / 主失败切次级 / 冷却期内不再探测主 / 冷却过后恢复即切回 /
双失败抛原异常 / 流式首块前可切换 / 用量聚合（§2.3 熔断依赖）。
"""

import asyncio

import pytest
from autogen_core.models import RequestUsage, UserMessage
from autogen_ext.models.replay import ReplayChatCompletionClient

from tripmate.llm import _PRIMARY_COOLDOWN_MAX_S, _PRIMARY_COOLDOWN_S, _FallbackClient

_MSG = lambda: UserMessage(content="测试问题", source="user")  # noqa: E731


class _FlakyClient(ReplayChatCompletionClient):
    """前 fail_times 次 create 抛错，之后返回预置回复（模拟主模型故障→恢复）。"""

    def __init__(self, reply: str, fail_times: int):
        super().__init__([reply])
        self.fail_times = fail_times
        self.calls = 0

    async def create(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("模拟主模型故障")
        return await super().create(messages, **kwargs)


def test_primary_success_no_failover():
    primary = ReplayChatCompletionClient(["主模型回复", "第二次回复"])
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    assert asyncio.run(client.create([_MSG()])).content == "主模型回复"
    assert client._on_primary
    assert asyncio.run(client.create([_MSG()])).content == "第二次回复"


def test_empty_content_retried_on_same_channel():
    """思考型模型偶发只回推理不回正文（content 空串）：同通道立即重试一次，不触发主备切换
    （2026-09-19 e2e 根因：结构化空回退/群聊消息清洗成占位均由此而来）。"""
    primary = ReplayChatCompletionClient(["", "重试后的回复"])
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    assert asyncio.run(client.create([_MSG()])).content == "重试后的回复"
    assert client._on_primary  # 空 content 属同通道重试，不是主模型故障，不得触发切换


class _NoneContentClient(ReplayChatCompletionClient):
    """第一次 create 返回 content=None（deepseek 反思轮实测形态：
    2026-09-19 用户会话 3/3 失败，autogen 抛 "Reflect on tool use"），之后返回预置回复。"""

    def __init__(self, reply: str):
        super().__init__([reply])
        self.calls = 0

    async def create(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            import types
            from autogen_core.models import RequestUsage
            return types.SimpleNamespace(
                content=None, finish_reason="stop", thought=None,
                usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                cached=False, logprobs=None)
        return await super().create(messages, **kwargs)


def test_none_content_retried_on_same_channel():
    """content=None 与空串同病同治：同通道立即重试一次，不触发主备切换。"""
    primary = _NoneContentClient("重试后的回复")
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    assert asyncio.run(client.create([_MSG()])).content == "重试后的回复"
    assert client._on_primary


class _EmptyToolRoundsClient(ReplayChatCompletionClient):
    """工具轮连续返回 content=None（2026-09-23 e2e 实测：新主模型聚合端点反思轮），
    无 tools 请求才返回文本（模拟末次去工具请求救回，端点对无 tools 请求稳定返文本）。"""

    def __init__(self, reply: str):
        super().__init__([reply])
        self.tool_rounds = 0

    async def create(self, messages, tools=None, tool_choice="auto", **kwargs):
        if tools:
            self.tool_rounds += 1
            import types
            from autogen_core.models import RequestUsage
            return types.SimpleNamespace(
                content=None, finish_reason="stop", thought=None,
                usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                cached=False, logprobs=None)
        return await super().create(messages, tools=tools, tool_choice=tool_choice, **kwargs)


def test_empty_tool_round_recovers_via_no_tools_request():
    """工具轮 3 次空 content 后放行一次无 tools 请求（2026-09-23：反思轮持续空回复令
    autogen 抛 "Reflect on tool use"，Chatter 路径有 nudge 兜底、团队路径没有——
    阶段直接猝死；末次去工具请求只求总结文本，工具结果已在上下文里）。"""
    from autogen_core.tools import ParametersSchema, ToolSchema
    tool = ToolSchema(name="t", description="d",
                      parameters=ParametersSchema(type="object", properties={}))
    primary = _EmptyToolRoundsClient("无 tools 总结")
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    result = asyncio.run(client.create([_MSG()], tools=[tool], tool_choice="auto"))
    assert result.content == "无 tools 总结"
    assert primary.tool_rounds == 3  # 初始 + 2 次同通道重试；第 4 次为放行的去工具请求
    assert client._on_primary  # 空 content 是同通道病症，不得触发主备切换


class _ListNoToolsClient(ReplayChatCompletionClient):
    """无 tools 的调用返回 FunctionCall 列表（deepseek 未遵守 tool_choice=none，
    2026-09-20 反思轮实测形态），之后返回预置文本回复。"""

    def __init__(self, reply: str):
        super().__init__([reply])
        self.calls = 0

    async def create(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            import types
            from autogen_core import FunctionCall
            from autogen_core.models import RequestUsage
            return types.SimpleNamespace(
                content=[FunctionCall(id="x", name="save_travel_info", arguments="{}")],
                finish_reason="tool_calls", thought=None,
                usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
                cached=False, logprobs=None)
        return await super().create(messages, **kwargs)


def test_no_tools_call_returning_list_retried_on_same_channel():
    """无 tools 调用返回 FunctionCall 列表 = 端点未遵守 tool_choice=none：重试一次。
    带 tools 的正常工具调用轮返回列表是合法产出，绝不受影响。"""
    primary = _ListNoToolsClient("重试后的回复")
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    assert asyncio.run(client.create([_MSG()])).content == "重试后的回复"
    assert client._on_primary


def test_primary_failure_switches_to_secondary():
    primary = _FlakyClient("主模型回复", fail_times=999)
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"]))
    assert asyncio.run(client.create([_MSG()])).content == "次级回复"
    assert not client._on_primary
    assert primary.calls == 1


def test_no_primary_probe_during_cooldown():
    primary = _FlakyClient("主模型回复", fail_times=999)
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"] * 5))
    asyncio.run(client.create([_MSG()]))
    assert asyncio.run(client.create([_MSG()])).content == "次级回复"
    assert primary.calls == 1  # 冷却期内主模型未被再次调用


def test_primary_recovers_after_cooldown(monkeypatch):
    primary = _FlakyClient("主模型回复", fail_times=1)
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"] * 5))
    # 首次调用：主失败 → 次级接管
    assert asyncio.run(client.create([_MSG()])).content == "次级回复"
    assert not client._on_primary
    # 冷却过后重新探测主模型，恢复即切回
    import time as _time
    real_monotonic = _time.monotonic
    monkeypatch.setattr(_time, "monotonic", lambda: real_monotonic() + _PRIMARY_COOLDOWN_S + 1)
    assert asyncio.run(client.create([_MSG()])).content == "主模型回复"
    assert client._on_primary and primary.calls == 2


def test_both_fail_raises_original_error():
    client = _FallbackClient(
        _FlakyClient("x", fail_times=999), _FlakyClient("y", fail_times=999)
    )
    with pytest.raises(RuntimeError, match="模拟主模型故障"):
        asyncio.run(client.create([_MSG()]))


def test_stream_fails_over_before_first_chunk():
    primary = _FlakyClient("x", fail_times=999)
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级流式回复"]))

    async def collect():
        chunks = []
        async for chunk in client.create_stream([_MSG()]):
            chunks.append(chunk)
        return chunks

    assert asyncio.run(collect())


def test_total_usage_aggregates_both_clients():
    primary = ReplayChatCompletionClient(["主模型回复", "第二次回复"])
    secondary = ReplayChatCompletionClient(["次级回复"])
    client = _FallbackClient(primary, secondary)
    asyncio.run(client.create([_MSG()]))
    asyncio.run(client.create([_MSG()]))
    expect = RequestUsage(
        prompt_tokens=primary.total_usage().prompt_tokens + secondary.total_usage().prompt_tokens,
        completion_tokens=primary.total_usage().completion_tokens + secondary.total_usage().completion_tokens,
    )
    got = client.total_usage()
    assert (got.prompt_tokens, got.completion_tokens) == (expect.prompt_tokens, expect.completion_tokens)


def test_primary_cooldown_grows_and_resets(monkeypatch):
    """自适应冷却回归（2026-09-05 e2e 修复）：主通道故障夜每轮固定 120s 后重探测、
    白付一次 150s 超时——连续失败（冷却到期后的重探测失败）时冷却 ×2 递增
    （600s 封顶），成功即复位。单次失败的既有行为不变
    （test_primary_recovers_after_cooldown 锁定）。"""
    primary = _FlakyClient("x", fail_times=999)
    client = _FallbackClient(primary, ReplayChatCompletionClient(["次级回复"] * 10))
    asyncio.run(client.create([_MSG()]))                    # 首次失败：streak=1
    assert client._cooldown() == _PRIMARY_COOLDOWN_S
    import time as _time
    real_monotonic = _time.monotonic
    offset = {"v": 0.0}
    monkeypatch.setattr(_time, "monotonic", lambda: real_monotonic() + offset["v"])
    # 真实时序：每次冷却到期 → 重探测主模型 → 再次失败 → 冷却翻倍
    for mult in (2, 3, 4, 4):
        offset["v"] += _PRIMARY_COOLDOWN_S * (2 ** (mult - 2)) + 1  # 跳出当前冷却期
        asyncio.run(client.create([_MSG()]))                # 冷却后的重探测再次失败
        assert client._cooldown() == min(_PRIMARY_COOLDOWN_S * (2 ** (mult - 1)),
                                         _PRIMARY_COOLDOWN_MAX_S)
    client._note_success(0)                                 # 主模型恢复 → 复位
    assert client._cooldown() == _PRIMARY_COOLDOWN_S
