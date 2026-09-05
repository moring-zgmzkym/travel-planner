"""稳定性加固回归第二组（2026-09-04）：LLM 连接错误快速重试 / Chatter 重建 /
二次规划幂等标志与笔记分区清理 / 网关补播崩溃防护。

背景（logs/tripmate.log）：09-04 09:52 主备两通道同轮各报一次 APIConnectionError，
用户直接收到失败提示且污染上下文残留；换城二次规划时旧城市笔记/封面泄漏进新 PDF。
全程 stub，不联网。
"""

import asyncio
import json

import httpx
import openai
import pytest
from autogen_core.models import UserMessage
from autogen_ext.models.replay import ReplayChatCompletionClient

import tripmate.session as session_mod
from tripmate.blackboard import Blackboard
from tripmate.llm import _FallbackClient
from tripmate.models import BasicInfo, Draft, DraftDay
from tripmate.status import StatusBus
from tripmate.team import TeamRunner

_MSG = lambda: UserMessage(content="测试问题", source="user")  # noqa: E731


class _ConnFlakyClient(ReplayChatCompletionClient):
    """前 conn_fail_times 次 create 抛连接类错误，之后返回预置回复。"""

    def __init__(self, reply: str, conn_fail_times: int, timeout_fail_times: int = 0):
        super().__init__([reply])
        self.conn_fail_times = conn_fail_times
        self.timeout_fail_times = timeout_fail_times
        self.calls = 0

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.timeout_fail_times:
            raise openai.APITimeoutError(request=httpx.Request("POST", "https://llm.test"))
        if self.calls <= self.timeout_fail_times + self.conn_fail_times:
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://llm.test"))

    async def create(self, messages, **kwargs):
        self._maybe_fail()
        return await super().create(messages, **kwargs)


# ---- B5：连接错误同通道快速重试 ----

def test_connection_error_quick_retry_same_channel(monkeypatch):
    """主模型一次连接错误 → 同通道重试后成功，次级通道零调用（用户需求：优先重连主模型）。"""
    monkeypatch.setattr("tripmate.llm._CONN_RETRY_DELAY_S", 0.01)  # 重试延迟不实睡
    primary = _ConnFlakyClient("主模型回复", conn_fail_times=1)
    secondary = _ConnFlakyClient("次级回复", conn_fail_times=0)
    secondary.calls = 0
    client = _FallbackClient(primary, secondary)
    assert asyncio.run(client.create([_MSG()])).content == "主模型回复"
    assert primary.calls == 2  # 一次连接错误 + 一次重试
    assert secondary.calls == 0  # 未发生通道切换


def test_connection_error_both_channels_fail_raises_original(monkeypatch):
    """双通道皆连接错误（09-04 09:52 实测形态）：重试后仍失败 → 抛原始异常（不吞错）。"""
    monkeypatch.setattr("tripmate.llm._CONN_RETRY_DELAY_S", 0.01)  # 重试延迟不实睡
    primary = _ConnFlakyClient("主模型回复", conn_fail_times=99)
    secondary = _ConnFlakyClient("次级回复", conn_fail_times=99)
    client = _FallbackClient(primary, secondary)
    with pytest.raises(openai.APIConnectionError):
        asyncio.run(client.create([_MSG()]))
    assert primary.calls == 2 and secondary.calls == 2  # 各自快速重试了一轮


def test_timeout_not_quick_retried_fails_over_directly():
    """超时已等满上限：不快速重试，直接切次级（APITimeoutError 是 APIConnectionError 子类，须排除）。"""
    primary = _ConnFlakyClient("主模型回复", conn_fail_times=0, timeout_fail_times=1)
    secondary = _ConnFlakyClient("次级回复", conn_fail_times=0)
    client = _FallbackClient(primary, secondary)
    assert asyncio.run(client.create([_MSG()])).content == "次级回复"
    assert primary.calls == 1  # 超时后未同通道重试
    assert not client._on_primary


# ---- B6：Chatter 重建 ----

def _make_session(monkeypatch, stream_impl):
    """构建不依赖真实 LLM 的 Session（chatter 构建替换为计数器，便于断言重建）。"""
    counter = iter(range(1000))

    async def fake_stream(chatter, text, source="user", seen_tools=None):
        return await stream_impl(chatter, text, source, seen_tools)

    monkeypatch.setattr(session_mod, "build_chatter", lambda *a, **k: next(counter))
    monkeypatch.setattr(session_mod, "stream_chatter", fake_stream)
    s = session_mod.Session()
    return s, s.chatter


def test_handle_user_message_rebuilds_chatter_on_llm_error(monkeypatch):
    """非超时 LLM 失败（双通道连接错误）：重建 Chatter 丢弃污染上下文，返回重发提示。"""
    async def boom(chatter, text, source, seen_tools):
        raise RuntimeError("主备双通道全挂")

    s, sentinel = _make_session(monkeypatch, boom)
    reply = asyncio.run(s.handle_user_message("你好"))
    assert "再发一次" in reply
    assert s.chatter is not sentinel  # 重建过


def test_relay_cancelled_rebuilds_chatter_and_reraises(monkeypatch):
    """转述被取消（重连时旧 sender 取消）：重建 Chatter 后透传 CancelledError。"""
    async def slow(chatter, text, source, seen_tools):
        await asyncio.sleep(30)

    s, sentinel = _make_session(monkeypatch, slow)

    async def main():
        task = asyncio.get_running_loop().create_task(s.relay_team_event("测试转述"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert s.chatter is not sentinel  # 重建过（污染上下文已丢弃）

    asyncio.run(main())


# ---- C9：二次规划幂等标志与笔记分区 ----

def _bb(destination: str) -> Blackboard:
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(
        origin="上海", destination=destination, days=3,
        travel_dates=["2026-10-01", "2026-10-03"], travel_mode="高铁", party_size=2)
    return bb


def test_start_resets_digest_cover_flags(monkeypatch):
    """新一轮规划：攻略提炼/封面检索幂等标志复位（上一轮失败过的通道可再尝试一次）。"""
    async def fake_phase(self, phase):
        return None

    monkeypatch.setattr(TeamRunner, "_phase_loop", fake_phase)
    bb = _bb("汉中")
    r = TeamRunner(bb, StatusBus())
    r._digest_done = True
    r._cover_done = True

    async def main():
        receipt = r.start()
        await asyncio.sleep(0)
        return receipt

    assert asyncio.run(main())["status"] == "accepted"
    assert r._digest_done is False and r._cover_done is False


def test_start_clears_notes_and_covers_on_destination_change(monkeypatch):
    """换城二次规划：旧城市的景点/美食笔记与封面图一并清空（此前混入新 PDF）。"""
    async def fake_phase(self, phase):
        return None

    monkeypatch.setattr(TeamRunner, "_phase_loop", fake_phase)
    bb = _bb("汉中")           # 画像已是新目的地
    bb.profile.draft = Draft(days=[DraftDay(
        date="2026-10-01", morning="旧上午", afternoon="旧下午", evening="旧晚上", spots=["旧景点"])])
    bb.profile.spot_notes = ["淄博旧景点笔记"]
    bb.profile.food_notes = ["淄博旧美食笔记"]
    bb.profile.cover_images = ["citycover_旧.png"]
    r = TeamRunner(bb, StatusBus())
    r._last_destination = "淄博"

    async def main():
        receipt = r.start()
        await asyncio.sleep(0)
        return receipt

    assert asyncio.run(main())["status"] == "accepted"
    assert bb.profile.spot_notes == [] and bb.profile.food_notes == []
    assert bb.profile.cover_images == []


def test_start_keeps_notes_on_same_destination(monkeypatch):
    """同目的地二次规划：笔记/封面与数据分区一样保留（不误清）。"""
    async def fake_phase(self, phase):
        return None

    monkeypatch.setattr(TeamRunner, "_phase_loop", fake_phase)
    bb = _bb("汉中")
    bb.profile.spot_notes = ["汉中旧景点笔记"]
    bb.profile.cover_images = ["citycover_汉中.png"]
    r = TeamRunner(bb, StatusBus())
    r._last_destination = "汉中"

    async def main():
        receipt = r.start()
        await asyncio.sleep(0)
        return receipt

    assert asyncio.run(main())["status"] == "accepted"
    assert bb.profile.spot_notes == ["汉中旧景点笔记"]
    assert bb.profile.cover_images == ["citycover_汉中.png"]


# ---- C7：网关补播崩溃防护 ----

def test_replay_crash_survives_and_reports(monkeypatch):
    """补播阶段草稿渲染崩溃：_replay_snapshot 返回 False（调用方关闭连接），
    不再烧掉 WS 处理器（此前会触发前端 2s 重连风暴）。"""
    import tripmate.gateway.app as app

    class FakeWS:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, raw: str):
            self.sent.append(json.loads(raw))

    s = app.sessions["default"]
    s.bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=2)
    s.bb.profile.draft = Draft(days=[DraftDay(
        date="2026-10-01", morning="熊猫基地", afternoon="宽窄巷子", evening="锦里",
        spots=["熊猫基地", "宽窄巷子"])], budget_total=1.0)

    def boom(sess):  # 与真实 _render_draft_html 同为同步函数，渲染期直接抛错
        raise RuntimeError("草稿渲染崩溃（模拟）")

    monkeypatch.setattr(app, "_render_draft_html", boom)

    async def main():
        ws = FakeWS()
        ok = await app._replay_snapshot(ws, "default", s)
        # 分级隔离（2026-09-05）：坏草稿只跳过卡片，历史/画像照常补播、连接保留
        assert ok is True
        assert any(m.get("type") == "session" for m in ws.sent)
        assert not any(m.get("type") == "draft" for m in ws.sent)

    asyncio.run(main())
