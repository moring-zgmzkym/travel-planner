"""前端状态持久化回归（2026-09-19）：subagent 灯态快照补发 / Token 面板补发 /
Commons 熔断 / 应用窗口 URL 归一化。全程离线。"""

import asyncio
import json

import httpx
import pytest

from tripmate.session import Session


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, raw: str):
        self.sent.append(json.loads(raw))


def _msg_types(ws: _FakeWS) -> list[str]:
    return [m.get("type") for m in ws.sent]


# ---- 快照补发：usage + sub_states ----

def test_replay_snapshot_sends_usage_and_sub_states(monkeypatch):
    """刷新/重连快照末尾补发 usage 与 sub_states——灯态与 Token 面板不再归零。"""
    import tripmate.gateway.app as app

    s = Session("default", username="tester")
    s.sub_states = {"guides": "done", "covers": "failed", "tickets": "running"}

    async def main():
        ws = _FakeWS()
        ok = await app._replay_snapshot(ws, "default", s)
        return ok, ws
    ok, ws = asyncio.run(main())

    assert ok is True
    types = _msg_types(ws)
    assert "usage" in types and "sub_states" in types
    # 顺序约束：sub_states 必须在 profile 之后（先回显画像，再覆盖灯态）
    assert types.index("sub_states") > types.index("profile")
    sub_msg = next(m for m in ws.sent if m.get("type") == "sub_states")
    assert sub_msg["states"] == {"guides": "done", "covers": "failed", "tickets": "running"}
    usage_msg = next(m for m in ws.sent if m.get("type") == "usage")
    assert "total_tokens" in usage_msg["usage"]


def test_replay_snapshot_without_run_has_empty_sub_states():
    """未跑过规划的会话：sub_states 为空 dict，消息仍发送（前端 no-op）。"""
    import tripmate.gateway.app as app

    s = Session("default", username="tester")

    async def main():
        ws = _FakeWS()
        ok = await app._replay_snapshot(ws, "default", s)
        return ok, ws
    ok, ws = asyncio.run(main())

    assert ok is True
    sub_msg = next(m for m in ws.sent if m.get("type") == "sub_states")
    assert sub_msg["states"] == {}


def test_sender_updates_sub_states_on_status_events():
    """_sender 消费 STATUS_SUBAGENT 更新真值；STATUS_PHASE/终态清空（与前端 reset 时机一致）。"""
    import tripmate.gateway.app as app
    from tripmate.status import StatusBus

    s = Session("default", username="tester")

    async def main():
        sub = s.bus.subscribe()
        task = asyncio.create_task(app._sender(_FakeWS(), s, sub=sub))
        await asyncio.sleep(0.01)
        await s.bus.emit("TeamRunner", "攻略 worker 启动", "STATUS_SUBAGENT", channel="guides", state="running")
        await s.bus.emit("TeamRunner", "攻略 worker 完成", "STATUS_SUBAGENT", channel="guides", state="done")
        await s.bus.emit("TeamRunner", "封面查询 worker 启动", "STATUS_SUBAGENT", channel="covers", state="running")
        await asyncio.sleep(0.05)
        assert s.sub_states == {"guides": "done", "covers": "running"}
        await s.bus.emit("TeamRunner", "规划团队启动", "STATUS_PHASE", phase="collect")
        await asyncio.sleep(0.05)
        assert s.sub_states == {}  # STATUS_PHASE 清空（与前端 resetSubLights 同步）
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())


# ---- Commons 熔断 ----

def test_commons_breaker_trips_on_connect_timeout(monkeypatch):
    """首次 ConnectTimeout 置位熔断：后续调用直接跳过，不再白等网络。"""
    import tripmate.tools.search as search

    monkeypatch.setattr(search, "_COMMONS_DISABLED", False)
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, url, params=None):
            calls["n"] += 1
            raise httpx.ConnectTimeout("网络不可达")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def main():
        first = await search.search_city_covers("成都")
        second = await search.search_city_covers("成都")
        return first, second
    first, second = asyncio.run(main())

    assert first == [] and second == []
    assert calls["n"] == 1, f"熔断后不应再发起 Commons 请求，实际调用 {calls['n']} 次"
    assert search._COMMONS_DISABLED is True
    search._COMMONS_DISABLED = False  # 复位，避免串扰其他用例


def test_commons_breaker_ignores_non_network_errors(monkeypatch):
    """非网络错误（如响应畸形）不触发熔断——单次失败不该永久关闭兜底。"""
    import tripmate.tools.search as search

    monkeypatch.setattr(search, "_COMMONS_DISABLED", False)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"query": {"pages": {}}}  # 空页集合：无候选但不报错

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get(self, url, params=None):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    async def main():
        await search.search_city_covers("成都")
        return search._COMMONS_DISABLED
    tripped = asyncio.run(main())

    assert tripped is False
    search._COMMONS_DISABLED = False


# ---- 应用窗口 URL 归一化（S1）----

def test_display_url_normalizes_wildcard_host(monkeypatch):
    """HOST=0.0.0.0 时 URL 归一化为 127.0.0.1——0.0.0.0 不是可访问地址，直接拼会白屏。"""
    from tripmate import app_window

    monkeypatch.setattr(app_window.ServerConfig, "HOST", "0.0.0.0")
    monkeypatch.setattr(app_window.ServerConfig, "PORT", 8011)
    assert app_window._display_url() == "http://127.0.0.1:8011"

    monkeypatch.setattr(app_window.ServerConfig, "HOST", "192.168.1.5")
    assert app_window._display_url() == "http://192.168.1.5:8011"
