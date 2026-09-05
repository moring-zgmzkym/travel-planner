"""稳定性加固回归（2026-09-04）：MCP/JobBoard 硬上限与抛弃语义、退避重试。

背景事故（logs/tripmate.log 09-03/09-04 两次）：MCP 传输 wedged 后 anyio 清理阶段
自行挂住，wait_for 超时要等任务真正退出才能返回 → 上层超时永不生效 → 酒店任务永久
挂起 → collect 阶段心跳空转 26-47 分钟直至杀进程。本组测试锁定：
- 单次 MCP 调用硬上限（超时取消 → 0.5s 宽限 → 抛弃，快速返回 ServiceUnavailable）
- JobBoard 单任务提交锚定总预算（收割拖延不放大等待；重跑清板预算全新）
- 收割方被取消时任务保持存活（旧 shield 语义不回归）
- with_retry 指数退避与外层取消透传
全程 stub，不联网。
"""

import asyncio
import json

import pytest

from tripmate.config import JobConfig, McpConfig
from tripmate.status import StatusBus
from tripmate.tools.mcp_client import McpSession
from tripmate.tools.resilience import (
    ServiceUnavailable,
    _backoff_delay,
    cancel_with_grace,
    with_retry,
)
from tripmate.team import JobBoard, TeamContext, TeamState, make_booking_tools, make_researcher_tools


# ---- with_retry：退避与取消 ----

def test_backoff_delay_grows_and_capped():
    for attempt in range(6):
        base = min(5.0 * (2 ** attempt), 30.0)
        assert base * 0.5 <= _backoff_delay(5.0, attempt) <= base * 1.5
    assert _backoff_delay(5.0, 10) <= 45.0  # 上限 30s × 抖动上限 1.5


def test_with_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("瞬态失败")
        return "ok"

    assert asyncio.run(with_retry(flaky, retries=2, delay_s=0.001, what="测试")) == "ok"
    assert calls["n"] == 3


def test_with_retry_exhausts_to_service_unavailable():
    async def always_fail():
        raise RuntimeError("持续失败")

    with pytest.raises(ServiceUnavailable):
        asyncio.run(with_retry(always_fail, retries=1, delay_s=0.001, what="测试"))


def test_with_retry_outer_cancel_propagates():
    async def slow():
        await asyncio.sleep(5)

    async def main():
        task = asyncio.get_running_loop().create_task(
            with_retry(slow, retries=5, delay_s=0.01, what="测试"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


# ---- JobBoard：提交锚定预算 + 取消/异常语义 ----

def _hang_job(seconds: float = 30):
    async def _job():
        await asyncio.sleep(seconds)
    return _job()


def _wedged_job():
    """模拟挂死任务：捕获取消后清理阶段长时间不退出（MCP/anyio 事故形态）。"""
    async def _job():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(1.0)
    return _job()


def _value_job(value):
    async def _job():
        return value
    return _job()


def test_jobboard_collect_timeout_raises_quickly(monkeypatch):
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.1)

    async def main():
        jb = JobBoard()
        jb.submit("tickets", _hang_job())
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(ServiceUnavailable):
            await jb.collect("tickets")
        assert asyncio.get_running_loop().time() - t0 < 5  # 远小于任务自身的 30s
        assert jb._jobs["tickets"].done()  # 可取消任务已被取消，非抛弃

    asyncio.run(main())


def test_jobboard_budget_anchored_at_submission(monkeypatch):
    """提交后拖延收割：等待的是剩余预算（900s − 已流逝），不是重新起算的全额。"""
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.3)

    async def main():
        jb = JobBoard()
        jb.submit("tickets", _hang_job())
        await asyncio.sleep(0.2)  # 已流逝约 2/3 预算
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(ServiceUnavailable):
            await jb.collect("tickets")
        assert asyncio.get_running_loop().time() - t0 < 0.2  # 只等剩余 ~0.1s

    asyncio.run(main())


def test_jobboard_clear_resets_budget(monkeypatch):
    """重跑清板（检查点增量重跑/新一轮规划）：重新提交的任务获得全新预算。"""
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.2)

    async def main():
        jb = JobBoard()
        jb.submit("tickets", _hang_job())
        await asyncio.sleep(0.15)  # 预算已消耗大半
        jb.clear()                 # 模拟 _run_phase 开头清板
        jb.submit("tickets", _value_job({"mode": "mock", "candidates": []}))
        await asyncio.sleep(0.15)  # 若沿用旧锚点此刻已超预算
        assert (await jb.collect("tickets"))["mode"] == "mock"  # 新预算内正常收割

    asyncio.run(main())


def test_jobboard_collect_preserves_result_and_exception():
    async def _value(v):
        return v

    async def _boom():
        raise RuntimeError("任务内部异常")

    async def main():
        jb = JobBoard()
        jb.submit("ok", _value(42))
        assert await jb.collect("ok") == 42
        jb.submit("bad", _boom())
        with pytest.raises(RuntimeError, match="任务内部异常"):
            await jb.collect("bad")

    asyncio.run(main())


def test_jobboard_cancelled_job_degrades():
    async def main():
        jb = JobBoard()
        jb.submit("tickets", _hang_job())
        jb._jobs["tickets"].cancel()
        with pytest.raises(ServiceUnavailable):
            await jb.collect("tickets")

    asyncio.run(main())


def test_jobboard_collect_survives_outer_cancel():
    """收割方被取消时任务保持存活（旧 shield 语义），可再次收割。"""
    async def _late():
        await asyncio.sleep(0.3)
        return {"mode": "mock", "candidates": []}

    async def main():
        jb = JobBoard()
        jb.submit("tickets", _late())
        collector = asyncio.get_running_loop().create_task(jb.collect("tickets"))
        await asyncio.sleep(0.02)
        collector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await collector
        assert not jb._jobs["tickets"].done()  # 任务未被连带取消
        assert (await jb.collect("tickets"))["mode"] == "mock"

    asyncio.run(main())


def test_jobboard_timeout_abandons_wedged_task(monkeypatch):
    """清理挂死的任务：取消 → 宽限内未退出 → 抛弃（快速返回，不等清理完成）。"""
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.1)

    async def main():
        jb = JobBoard()
        jb.submit("hotels", _wedged_job())
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(ServiceUnavailable):
            await jb.collect("hotels")
        assert asyncio.get_running_loop().time() - t0 < 2  # 0.1 预算 + 0.5 宽限，而非 1s 清理

    asyncio.run(main())


# ---- McpSession：单次调用硬上限 ----

def _hung_open(self):
    async def _open():
        await asyncio.sleep(30)
    return _open()


def test_mcp_call_hard_cap_on_hung_open(monkeypatch):
    monkeypatch.setattr(McpConfig, "CALL_TIMEOUT_S", 0.15)
    monkeypatch.setattr(McpSession, "_open", _hung_open)
    s = McpSession("sse", url="https://example.com/sse")

    async def main():
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(ServiceUnavailable):
            await s.call(("x",), {}, what="测试通道")
        assert asyncio.get_running_loop().time() - t0 < 3

    asyncio.run(main())


def test_mcp_call_outer_cancel_stops_inner(monkeypatch):
    """用户停止/阶段取消：内部任务被一并取消并透传 CancelledError。"""
    monkeypatch.setattr(McpConfig, "CALL_TIMEOUT_S", 60)
    monkeypatch.setattr(McpSession, "_open", _hung_open)
    s = McpSession("sse", url="https://example.com/sse")

    async def main():
        t = asyncio.get_running_loop().create_task(s.call(("x",), {}, what="测试通道"))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())


# ---- 收割工具逐通道降级 ----

def test_collect_booking_results_degrades_on_hung_channels(monkeypatch):
    """车票/酒店通道任务挂死：仅该通道降级空候选，收割工具仍返回完整摘要。"""
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.1)

    async def main():
        from tripmate.blackboard import Blackboard
        bb = Blackboard()
        jb = JobBoard()
        jb.submit("tickets", _hang_job())
        jb.submit("hotels", _hang_job())
        ctx = TeamContext(bb=bb, bus=StatusBus(), state=TeamState(), jobs=jb,
                          runner=None, run_id="t")
        collect_tool = make_booking_tools(ctx)[1]
        data = json.loads(await collect_tool())
        assert data["ticket_candidates"] == [] and data["hotel_candidates"] == []
        assert any("车票通道" in n for n in data["notes"])
        assert any("酒店通道" in n for n in data["notes"])
        assert data["weather_days"] == []

    asyncio.run(main())


def test_finish_guide_search_degrades_on_hung_job(monkeypatch):
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.1)

    async def main():
        from tripmate.blackboard import Blackboard
        bb = Blackboard()
        jb = JobBoard()
        jb.submit("guides", _hang_job())
        ctx = TeamContext(bb=bb, bus=StatusBus(), state=TeamState(), jobs=jb,
                          runner=None, run_id="t")
        finish_tool = make_researcher_tools(ctx)[1]
        data = json.loads(await finish_tool())
        assert data["status"] == "error" and "护栏" in data["error"]

    asyncio.run(main())


# ---- cancel_with_grace ----

def test_cancel_with_grace_clean_task():
    async def main():
        t = asyncio.get_running_loop().create_task(asyncio.sleep(30))
        await asyncio.sleep(0.01)
        assert await cancel_with_grace(t, grace_s=0.1) is False
        assert t.cancelled()

    asyncio.run(main())


def test_cancel_with_grace_abandons_wedged_task():
    async def main():
        t = asyncio.get_running_loop().create_task(_wedged_job())
        await asyncio.sleep(0.01)
        assert await cancel_with_grace(t, grace_s=0.05) is True
        assert not t.done()  # 已抛弃：不再等待其清理完成

    asyncio.run(main())


# ---- PersistentMcpSession / 阶段级会话池（2026-09-05 阶段二）----

def _fake_tool(name="ticketx"):
    import types
    return types.SimpleNamespace(name=name, input_schema={"properties": {"a": {}}})


def _fake_result(payload='{"ok": 1}'):
    import types
    return types.SimpleNamespace(
        isError=False,
        content=[types.SimpleNamespace(type="text", text=payload)])


def test_pool_reuses_session_and_falls_back_to_short_session():
    """池启用：同通道工厂返回同一持久会话；池关闭后回退原短会话路径。"""
    import asyncio
    from tripmate.tools import mcp_client as m

    m.begin_mcp_pool()
    try:
        s1 = m.train_session()
        s2 = m.train_session()
        assert s1 is s2 and isinstance(s1, m.PersistentMcpSession)
        assert m._pool_get("12306") is s1
    finally:
        asyncio.run(m.end_mcp_pool())
    assert m._ACTIVE_POOL is None
    s3 = m.train_session()
    assert type(s3) is m.McpSession  # 池未启用 → 原短会话行为（每次调用全新握手）


def test_persistent_call_success_keeps_session():
    import asyncio
    import types
    from tripmate.tools.mcp_client import PersistentMcpSession

    s = PersistentMcpSession("stdio", name="t")
    s._tools = [_fake_tool()]
    s._session = types.SimpleNamespace(call_tool=_fake_result_call)

    async def _fake_call(self, keywords, args, what):
        return _fake_result()

    async def main():
        out = await s.call(("ticket",), {"a": 1}, "t")
        assert out == {"ok": 1}
        # 成功路径不重置：后续调用继续复用（这正是"多次调用一次握手"的机制）
        assert s._session is not None and s._tools

    asyncio.run(main())


async def _fake_result_call(name, args):
    return _fake_result()


def test_persistent_call_resets_session_on_tool_error():
    """调用失败（含 isError）→ 会话重置：下次调用重新握手，重试归外层 with_retry。"""
    import asyncio
    import types
    import pytest
    from tripmate.tools.mcp_client import PersistentMcpSession
    from tripmate.tools.resilience import ServiceUnavailable

    def _boom(name, args):
        raise ServiceUnavailable("MCP 工具执行报错：车次不存在")

    s = PersistentMcpSession("stdio", name="t")
    s._tools = [_fake_tool()]
    s._session = types.SimpleNamespace(call_tool=_boom)

    async def main():
        try:
            await s.call(("ticket",), {"a": 1}, "t")
            raised = False
        except ServiceUnavailable:
            raised = True
        assert raised

    asyncio.run(main())
    assert s._session is None and s._tools is None  # 重置生效（下次调用重新握手）


def test_persistent_handshake_timeout_independent(monkeypatch):
    """握手超时独立预算：npx 冷启动慢时抛握手超时，不挤占单次调用 90s 预算。"""
    import asyncio
    import pytest
    from tripmate.tools import mcp_client as m

    monkeypatch.setattr(m.McpConfig, "HANDSHAKE_TIMEOUT_S", 0.1)

    s = m.PersistentMcpSession("stdio", name="t")

    async def slow_open():
        await asyncio.sleep(5)

    monkeypatch.setattr(s, "_ensure_open", slow_open)

    async def main():
        try:
            await s.call(("ticket",), {}, "t")
            raised = False
        except m.ServiceUnavailable as e:
            raised = "握手" in str(e)
        assert raised

    asyncio.run(main())
    assert s._session is None  # 已重置
