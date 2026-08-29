"""selector 状态机与 TeamRunner 契约单测（§3.3/§3.4、风险 #5）。"""

import asyncio

from tripmate.blackboard import Blackboard
from tripmate.models import BasicInfo, Draft, DraftDay
from tripmate.status import StatusBus
from tripmate.team import (AGENT_MCP, AGENT_PLANNER, AGENT_PROC, AGENT_RES,
                           TeamRunner, TeamState)


def test_speaker_map_protocol():
    # 对等协议的发言顺序：广播 → 双方启动 → 双方收割 → 汇总 → 规划
    order = ["PROC_BROADCAST", "RES_START", "MCP_START", "RES_COLLECT", "MCP_COLLECT",
             "PROC_SUMMARIZE", "PLAN_DRAFT"]
    from tripmate.team import SPEAKER
    speakers = [SPEAKER[s] for s in order]
    assert speakers == [AGENT_PROC, AGENT_RES, AGENT_MCP, AGENT_RES, AGENT_MCP, AGENT_PROC, AGENT_PLANNER]


def test_selector_consecutive_cap():
    runner = TeamRunner(Blackboard(), StatusBus())
    runner._state = TeamState(phase="collect", step="RES_START")  # 一直选 Researcher
    picks = [runner._selector([]) for _ in range(5)]
    # 前 3 次照常选 Researcher，第 4 次触发连续发言保护 → 强制收敛到 Planner
    assert picks[:3] == [AGENT_RES] * 3
    assert picks[3] == AGENT_PLANNER and runner._state.consecutive == 1


def test_start_rejected_without_required():
    runner = TeamRunner(Blackboard(), StatusBus())
    receipt = runner.start()
    assert receipt["status"] == "rejected" and "出发地" in receipt["reason"]


def test_start_accepted_and_receipt_contract():
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3,
                                      travel_mode="高铁", budget=6000)
    runner = TeamRunner(bb, StatusBus())

    async def noop(*a, **k):
        return None

    async def main():
        runner._phase_loop = noop  # type: ignore[method-assign]
        receipt = runner.start()
        assert receipt["status"] == "accepted" and receipt["run_id"]
        # 二次启动被拒（团队运行中）
        assert runner.start()["status"] == "rejected"

    asyncio.run(main())


def test_draft_rounds_limit():
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=3)
    bb.profile.draft = Draft(days=[DraftDay(date="2026-10-01", spots=["宽窄巷子"])])
    runner = TeamRunner(bb, StatusBus())

    async def noop(*a, **k):
        return None

    async def main():
        runner._phase_loop = noop  # type: ignore[method-assign]
        for _ in range(3):
            assert runner.submit_feedback("修改意见", confirmed=False)["status"] == "accepted"
            await asyncio.sleep(0)  # 让 noop 后台任务完成（模拟阶段结束）
        # 第 4 次修改被拒（≤3 轮，§4.5）
        assert runner.submit_feedback("还想改", confirmed=False)["status"] == "rejected"

    asyncio.run(main())
