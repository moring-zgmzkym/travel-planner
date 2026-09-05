"""聊天可靠交付 + 会话持久化回归（2026-09-05）。

背景：09-04 下午"信息不补发给用户"——处理期间刷新页面，迟到的回复发往已死旧连接；
发送端离线静默丢弃；切换会话丢示例输入；服务重启内存会话全空。本组锁定：
- 持久化往返（聊天历史+画像恢复；changelog 置空、版本号保留；非法 sid 永不产生穿越路径）
- _chat 路由到会话当前活动连接（旧处理协程的迟到回复送达刷新后的新连接）
- sender 协作交接（状态事件零重复、团队事件零丢失、空闲超时退出）
- 补播分级隔离（坏草稿只跳过卡片，不关连接）
全程 stub，不联网；涉及 default 会话的用例均短路 save_session，不污染开发者落盘数据。
"""

import asyncio
import json

import pytest

from tripmate.config import SESSIONS_DIR
from tripmate.models import BasicInfo, Draft, DraftDay
from tripmate.persistence import safe_sid, session_path
from tripmate.status import StatusBus


class _FakeWS:
    def __init__(self, name: str = ""):
        self.name = name
        self.sent: list[dict] = []
        self.closed = False

    async def send_text(self, raw: str):
        self.sent.append(json.loads(raw))


class _BoomWS:
    async def send_text(self, *_):
        raise RuntimeError("旧连接已死")


def _no_save(monkeypatch):
    """default 会话被测试复用：短路落盘，不污染开发者 sessions/default.json。"""
    monkeypatch.setattr("tripmate.session.save_session", lambda *a, **k: None)


def _fill_draft(bb):
    bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=2)
    bb.profile.draft = Draft(
        days=[DraftDay(date="2026-10-01", morning="熊猫基地", afternoon="宽窄巷子",
                       evening="锦里", spots=["熊猫基地"])], budget_total=1.0)
    return bb


# ---- safe_sid 三分支 ----

def test_safe_sid_branches():
    assert safe_sid("") == "default"
    assert safe_sid(None) == "default"
    assert safe_sid(" s-abc123 ") == "s-abc123"
    key = safe_sid("../../evil")
    assert key.startswith("junk-") and len(key) == len("junk-") + 8
    assert safe_sid("../../evil") == key  # 确定性：同一非法 sid 稳定映射（刷新不丢历史）


def test_unsafe_sid_never_escapes_sessions_dir():
    p = session_path("../../evil")
    assert p.parent == SESSIONS_DIR
    assert p.name.startswith("junk-") and p.name.endswith(".json")
    assert ".." not in p.name


# ---- 持久化往返 ----

def test_persistence_roundtrip():
    sid = "t-roundtrip-1"
    session_path(sid).unlink(missing_ok=True)
    try:
        from tripmate.session import Session
        s = Session(sid)
        s.bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3)
        draft = Draft(days=[DraftDay(date="2026-10-01", morning="m", afternoon="a",
                                     evening="e", spots=["s"])], budget_total=1.0)

        async def main():
            s.record_chat({"type": "chat", "role": "user", "text": "帮我规划成都"})
            await s.bb.write("draft", draft, "planner", "测试草稿")
            await s.flush()  # 异步单飞落盘（2026-09-05 去阻塞）：断言前显式冲刷

        asyncio.run(main())
        version = s.bb.version()
        assert session_path(sid).exists()  # record_chat 与黑板写都触发（合并）落盘

        s2 = Session(sid)  # 模拟服务重启后的新 Session
        assert [m["text"] for m in s2.chat_history()] == ["帮我规划成都"]
        assert s2.bb.profile.basic_info.destination == "成都"
        assert s2.bb.profile.draft is not None and s2.bb.profile.draft.budget_total == 1.0
        assert s2.bb.version() == version       # 版本号保留
        assert s2.bb.profile.changelog == []    # changelog 置空（防文件膨胀；重启后基线重置）
    finally:
        session_path(sid).unlink(missing_ok=True)


def test_session_without_sid_never_saves(monkeypatch):
    """无 sid（测试构造模式）：record_chat 短路，不触发任何落盘。"""
    from tripmate.session import Session
    calls = {"n": 0}
    monkeypatch.setattr("tripmate.session.save_session",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    s = Session()
    s.record_chat({"type": "chat", "role": "user", "text": "x"})
    assert calls["n"] == 0 and s.sid == ""


def test_default_session_carries_sid():
    """【关键修正回归】default 会话必须带 sid 构造，否则无 ?sid 的单用户场景永不落盘。"""
    import tripmate.gateway.app as app
    assert app.sessions["default"].sid == "default"


# ---- 迟到回复送达当前活动连接 ----

def test_late_reply_reaches_live_connection(monkeypatch):
    _no_save(monkeypatch)
    import tripmate.gateway.app as app

    s = app.sessions["default"]
    new_ws = _FakeWS("new")
    s._live_ws = new_ws  # 模拟：用户已刷新，旧处理协程还在跑
    msg = {"type": "chat", "role": "chatter", "text": "迟到的回复"}
    try:
        asyncio.run(app._chat(_BoomWS(), s, msg))  # 旧协程用自己的（已死）socket 发送
        assert new_ws.sent == [msg]               # 实际送达的是当前活动连接
        assert s.chat_history()[-1] == msg        # 且已记录（重连补播兜底）
    finally:
        s._live_ws = None


# ---- sender 协作交接 ----

def test_sender_handoff_no_duplicate_no_loss(monkeypatch):
    """交接重叠期：状态事件不重复推送（stale 丢副本），团队事件零丢失（送达当前连接）。"""
    _no_save(monkeypatch)
    import tripmate.gateway.app as app

    s = app.sessions["default"]
    _fill_draft(s.bb)

    async def ok_relay(note):
        return "转述文本"

    monkeypatch.setattr(s, "relay_team_event", ok_relay)

    async def main():
        ws_a, ws_b = _FakeWS("A"), _FakeWS("B")
        s._live_ws = ws_a
        task_a = asyncio.create_task(app._sender(ws_a, s))  # A 认领
        await asyncio.sleep(0.02)
        s._live_ws = ws_b
        task_b = asyncio.create_task(app._sender(ws_b, s))  # B 认领 → A stale
        await asyncio.sleep(0.02)
        try:
            await s.bus.emit("Researcher", "交接期状态事件", "STATUS_COLLECT")  # 两队列各有副本
            s.team_events.put_nowait(("draft_ready", s.bb.profile.draft))       # 单消费者队列
            async def wait_draft():
                while not any(m.get("type") == "draft" for m in ws_b.sent):
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(wait_draft(), timeout=3)
            await asyncio.sleep(0.1)                                        # 给 A 处理/退出留时间
            await s.bus.emit("TeamRunner", "存活验证", "STATUS_PROGRESS")   # A 应已退出，B 唯一送达
            await asyncio.sleep(0.05)
        finally:
            task_a.cancel()
            task_b.cancel()
            await asyncio.gather(task_a, task_b, return_exceptions=True)
            s._live_ws = None          # 清理共享 default 会话状态，防污染后续测试
            s.bb.profile.draft = None
        return ws_a, ws_b

    ws_a, ws_b = asyncio.run(main())
    all_sent = ws_a.sent + ws_b.sent
    assert len([m for m in all_sent if m.get("type") == "draft"]) == 1   # 团队事件零丢失零重复
    assert len([m for m in all_sent if m.get("type") == "status"
                and "交接期状态事件" in m.get("text", "")]) == 1          # 状态事件零重复
    assert len([m for m in all_sent if m.get("type") == "status"
                and "存活验证" in m.get("text", "")]) == 1                # A 退出后仅 B 送达
    # 转述回复经 _chat 路由送达当前连接（B）且已记录
    assert any(m.get("type") == "chat" and m.get("text") == "转述文本" for m in ws_b.sent)


def test_sender_idle_timeout_exits(monkeypatch):
    """空闲超时兜底：被接管的 stale sender 在超时后自行退出（防僵尸订阅累积）。"""
    _no_save(monkeypatch)
    import tripmate.gateway.app as app
    monkeypatch.setattr(app, "_SENDER_IDLE_TIMEOUT_S", 0.05)

    s = app.sessions["default"]

    async def main():
        ws_a = _FakeWS("A")
        task_a = asyncio.create_task(app._sender(ws_a, s))
        await asyncio.sleep(0.02)
        s._sender_task = object()  # 模拟新连接认领（异己对象）
        await asyncio.sleep(0.3)   # A 超时醒来 → stale → 退出
        assert task_a.done()

    asyncio.run(main())


# ---- 补播分级隔离 ----

def test_replay_skips_broken_draft_keeps_connection(monkeypatch):
    """坏草稿（持久化后跨重启存活）：只跳过草稿卡片，历史/画像照常补播、连接保留。"""
    import tripmate.gateway.app as app

    s = app.sessions["default"]
    _fill_draft(s.bb)

    def boom(sess):
        raise RuntimeError("草稿渲染崩溃（模拟）")

    monkeypatch.setattr(app, "_render_draft_html", boom)

    async def main():
        ws = _FakeWS()
        ok = await app._replay_snapshot(ws, "default", s)
        assert ok is True  # 连接保留（不再因单张坏卡片关闭）
        assert any(m.get("type") == "session" for m in ws.sent)
        assert not any(m.get("type") == "draft" for m in ws.sent)  # 坏卡片被跳过
        assert not ws.closed

    asyncio.run(main())


def test_replay_history_failure_closes_connection(monkeypatch):
    """核心历史补播失败（连接不可信）：返回 False 由调用方关闭。"""
    import tripmate.gateway.app as app

    s = app.sessions["default"]

    def boom():
        raise RuntimeError("画像快照崩溃（模拟）")

    monkeypatch.setattr(s, "profile_snapshot", boom)

    async def main():
        ws = _FakeWS()
        ok = await app._replay_snapshot(ws, "default", s)
        assert ok is False

    asyncio.run(main())


def test_flush_coalesces_writes(monkeypatch):
    """单飞合并：多次触发只做一次实际写盘（在途置脏 → 尾随补写收敛）。"""
    import asyncio
    from tripmate.session import Session
    calls = {"n": 0}

    saved = []

    def fake_save(sid, chats, profile):
        calls["n"] += 1
        saved.append(profile)

    monkeypatch.setattr("tripmate.session.save_session", fake_save)
    s = Session("t-coalesce")
    s.sid = "t-coalesce"  # 确保落盘路径启用

    async def main():
        s.record_chat({"type": "chat", "role": "user", "text": "1"})
        await s.bb.apply_basic_info({"origin": "上海"}, "chatter", "t")
        await s.bb.apply_basic_info({"budget": 5000.0}, "chatter", "t")
        await s.flush()

    asyncio.run(main())
    # 3 次触发 → 2 次写：record_chat 起飞的在途写期间，两次黑板写被合并为尾随一次。
    # （同步落盘时代是 3 次全量写；关键性质是写入次数不随触发次数线性膨胀且终态完整）
    assert calls["n"] == 2
    assert saved[-1]["basic_info"]["origin"] == "上海"
    assert saved[-1]["basic_info"]["budget"] == 5000.0


def test_cancel_current_chat_task():
    """stop 联动：取消正在进行的消息处理任务（修复 #10：处理期间 stop 可达）。"""
    import asyncio
    from tripmate.gateway.app import _cancel_current_chat
    from tripmate.session import Session

    s = Session()

    async def main():
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(slow())
        s.current_chat_task = task
        await asyncio.wait_for(started.wait(), timeout=1)
        assert _cancel_current_chat(s) is True
        with_state = asyncio.Event()
        try:
            await task
        except asyncio.CancelledError:
            with_state.set()
        await asyncio.wait_for(with_state.wait(), timeout=1)
        assert task.cancelled()
        s.current_chat_task = None
        assert _cancel_current_chat(s) is False  # 无在途任务 → 幂等

    asyncio.run(main())
