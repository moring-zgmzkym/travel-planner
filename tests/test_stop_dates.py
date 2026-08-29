"""停止按钮与出行时间段必问的回归测试（2026-08-29 五项运行问题修复）。"""

import asyncio
import json

from tripmate.blackboard import Blackboard
from tripmate.chatter import DEFAULTS, ensure_travel_dates
from tripmate.models import BasicInfo
from tripmate.status import StatusBus
from tripmate.team import AGENT_RES, TeamRunner, TeamState, make_researcher_tools
from tripmate.team import TeamContext


def test_defaults_exclude_date_text():
    """出行时间段不得静默默认——它是必问项，只能在追问后或启动兜底时写入。"""
    assert "date_text" not in DEFAULTS


def test_ensure_travel_dates_fills_default_once():
    async def main():
        bus = StatusBus()
        bb = Blackboard()
        bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3)
        # 无日期无标注 → 兜底写入"近期"并记录默认值标注
        assert await ensure_travel_dates(bb, bus) is True
        assert bb.profile.basic_info.date_text == "近期"
        assert "出行时间默认近期" in bb.profile.basic_info.defaults_applied
        # 已有标注 → 幂等不重复写
        version_before = bb.version()
        assert await ensure_travel_dates(bb, bus) is False
        assert bb.version() == version_before
        # 用户给了明确日期 → 不动
        bb2 = Blackboard()
        bb2.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=2,
                                           travel_dates=["2026-10-01", "2026-10-02"])
        assert await ensure_travel_dates(bb2, bus) is False
        assert bb2.profile.basic_info.date_text is None

    asyncio.run(main())


def test_researcher_tools_merged():
    """collect+write 已合并为 finish_guide_search（降低 LLM 工具调用次数要求）。"""
    ctx = TeamContext(bb=Blackboard(), bus=StatusBus(), state=TeamState(),
                      jobs=None, runner=None, run_id="t")
    names = {t.__name__ for t in make_researcher_tools(ctx)}
    assert names == {"start_guide_search", "finish_guide_search", "search_spot_images"}


def test_finish_guide_search_writes_and_advances():
    async def main():
        bb = Blackboard()
        bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3,
                                          travel_mode="高铁")
        bus = StatusBus()
        from tripmate.team import JobBoard
        ctx = TeamContext(bb=bb, bus=bus, state=TeamState(phase="collect", step="RES_COLLECT"),
                          jobs=JobBoard(), runner=None, run_id="t")
        tools = {t.__name__: t for t in make_researcher_tools(ctx)}

        # 场景：start 未执行（自愈提交）→ finish 留空参数走原始结果
        result = json.loads(await tools["finish_guide_search"](""))
        assert result["status"] == "written"
        assert len(bb.profile.guide_digest) >= 1
        assert ctx.state.step == "PROC_SUMMARIZE"
        assert all(g.reference_only for g in bb.profile.guide_digest)  # 降级数据必标注

        # 复用场景：变更影响分析判定攻略未受影响（reuse 开关开启）→ 走缓存不重搜
        ctx.reuse = {"guides": True}
        result2 = json.loads(await tools["finish_guide_search"](""))
        assert result2["status"] == "reused"

    asyncio.run(main())


def test_runner_cancel_states():
    async def main():
        bb = Blackboard()
        bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3)
        runner = TeamRunner(bb, StatusBus())

        # 空闲态：cancel 返回 idle，不抛异常
        receipt = runner.cancel()
        assert receipt["status"] == "idle"
        await asyncio.sleep(0)  # 让 emit 任务跑完
        assert any(e["kind"] == "STATUS_INFO" for e in runner.bus.history())

        # 运行态：cancel 终止任务、清理任务板、active 复位
        started = asyncio.Event()

        async def fake_phase(*a, **k):
            started.set()
            await asyncio.sleep(300)

        runner._phase_loop = fake_phase  # type: ignore[method-assign]
        receipt = runner.start()
        assert receipt["status"] == "accepted"
        await asyncio.wait_for(started.wait(), timeout=2)
        receipt = runner.cancel()
        assert receipt["status"] == "cancelled"
        await asyncio.sleep(0.05)
        assert runner._task.cancelled() or runner._task.done()
        assert runner.active is False and runner._awaiting_feedback is False
        assert runner._jobs.has("anything") is False
        kinds = [e["kind"] for e in runner.bus.history()]
        assert "STATUS_CANCELLED" in kinds

        # 取消后可重新启动
        runner._phase_loop = fake_phase  # type: ignore[method-assign]
        receipt = runner.start()
        assert receipt["status"] == "accepted"
        runner.cancel()

    asyncio.run(main())
