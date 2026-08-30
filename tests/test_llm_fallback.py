"""主备模型自动切换（llm._FallbackClient）单测：stub 客户端，不联网。

覆盖：主正常不切换 / 主失败切次级 / 冷却期内不再探测主 / 冷却过后恢复即切回 /
双失败抛原异常 / 流式首块前可切换 / 用量聚合（§2.3 熔断依赖）。
"""

import asyncio

import pytest
from autogen_core.models import RequestUsage, UserMessage
from autogen_ext.models.replay import ReplayChatCompletionClient

from tripmate.llm import _PRIMARY_COOLDOWN_S, _FallbackClient

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
