"""转述降级与画像视图单测（修复"草稿产出后用户看不到成果"的回归防护）。

- _profile_view 必须携带草稿/攻略/成品摘要（Chatter 才能转述，修复"has_draft=true 但拉不到文本"）。
- relay_team_event 超时/异常必须降级为文案、不抛异常、chatter_lock 不卡死。
- 网关 _sender 在 relay 抛异常（模拟 429 场景）时必须存活且草稿卡片照常送达。
"""

import asyncio
import json

import pytest

import tripmate.session as session_mod
from tripmate.blackboard import Blackboard
from tripmate.chatter import _profile_view
from tripmate.models import (BasicInfo, Draft, DraftDay, GuideDigestItem)


def _fill_draft(bb: Blackboard) -> Blackboard:
    """在黑板对象上就地填充草稿/攻略（Blackboard.profile 无 setter，不能整对象替换）。"""
    bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=2)
    bb.profile.draft = Draft(
        days=[DraftDay(date="2026-10-01", morning="熊猫基地", afternoon="宽窄巷子",
                       evening="锦里", spots=["熊猫基地", "宽窄巷子"])],
        budget_total=1234.5, warnings=["预算占用 95% 预警"])
    bb.profile.guide_digest = [GuideDigestItem(
        source_name="小红书", source_url="https://x", fetched_at="2026-08-29 10:00",
        spots=["大熊猫基地", "宽窄巷子"], foods=["火锅"], warnings=["熊猫基地要早去"])]
    return bb


def test_profile_view_contains_draft_guides_final():
    view = json.loads(_profile_view(_fill_draft(Blackboard())))
    assert "熊猫基地" in view["draft_summary"]
    assert view["draft_budget"]["total"] == 1234.5
    assert view["draft_budget"]["warnings"] == ["预算占用 95% 预警"]
    assert view["guide_highlights"]["spots"][0] == "大熊猫基地"
    assert view["guide_highlights"]["foods"] == ["火锅"]
    assert view["guide_highlights"]["warnings"] == ["熊猫基地要早去"]
    assert view["has_draft"] is True


def test_profile_view_empty_blackboard():
    view = json.loads(_profile_view(Blackboard()))
    assert "draft_summary" not in view and "guide_highlights" not in view
    assert view["has_draft"] is False


def _make_session(monkeypatch):
    """构建不依赖真实 LLM 的 Session（chatter 构建替换为计数器，便于断言重建）。"""
    counter = iter(range(1000))
    monkeypatch.setattr(session_mod, "build_chatter", lambda *a, **k: next(counter))
    s = session_mod.Session()
    return s, s.chatter


def test_relay_timeout_degrades_and_lock_released(monkeypatch):
    s, sentinel = _make_session(monkeypatch)
    monkeypatch.setattr(session_mod, "RELAY_TIMEOUT_S", 0.05)

    async def slow_stream(*a, **k):
        await asyncio.sleep(30)  # 模拟 LLM 限流长重试

    monkeypatch.setattr(session_mod, "stream_chatter", slow_stream)

    async def main():
        reply = await s.relay_team_event("测试转述")   # 不应抛异常
        assert "卡片" in reply
        assert s.chatter is not sentinel              # 降级分支已重建 Chatter
        # 锁未卡死：正常流立即可用
        async def fast_stream(*a, **k):
            return "OK"
        monkeypatch.setattr(session_mod, "stream_chatter", fast_stream)
        assert await asyncio.wait_for(s.relay_team_event("再来一次"), timeout=2) == "OK"

    asyncio.run(main())


def test_relay_success_passthrough(monkeypatch):
    s, _ = _make_session(monkeypatch)

    async def ok_stream(*a, **k):
        return "已转述"

    monkeypatch.setattr(session_mod, "stream_chatter", ok_stream)
    assert asyncio.run(s.relay_team_event("x")) == "已转述"


def test_sender_survives_relay_crash(monkeypatch):
    """集成回归：relay 抛异常（复现 429 烧协程场景）→ 草稿卡片仍送达、推送协程存活。"""
    import tripmate.gateway.app as app

    class FakeWS:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_text(self, raw: str):
            self.sent.append(json.loads(raw))

    s = app.sessions["default"]  # 需求 2：会话注册表（原全局 session）
    _fill_draft(s.bb)

    async def boom(*a, **k):
        raise RuntimeError("429 FreeUsageLimitError（模拟）")

    monkeypatch.setattr(s, "relay_team_event", boom)

    async def main():
        ws = FakeWS()
        sender = asyncio.create_task(app._sender(ws, s))
        try:
            await asyncio.sleep(0.05)
            s.team_events.put_nowait(("draft_ready", s.bb.profile.draft))
            # 草稿卡片必须送达（在 relay 失败的情况下）
            async def has_draft():
                while not any(m.get("type") == "draft" for m in ws.sent):
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(has_draft(), timeout=3)
            # 转述失败 → 降级错误进入时间线（STATUS_ERROR）
            async def has_error():
                while not any(m.get("type") == "status" and m.get("kind") == "STATUS_ERROR"
                              for m in ws.sent):
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(has_error(), timeout=3)
            # 协程存活：后续状态事件仍能送达
            await s.bus.emit("Researcher", "存活验证", "STATUS_COLLECT")
            async def has_alive():
                while not any(m.get("type") == "status" and "存活验证" in m.get("text", "")
                              for m in ws.sent):
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(has_alive(), timeout=3)
            assert not sender.done()
        finally:
            sender.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sender

    asyncio.run(main())
