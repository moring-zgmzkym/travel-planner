"""僵尸团队治理回归（2026-09-05 修复 #1）：熔断/取消后群聊 runtime 必须真正停止。

此前 run_stream 生成器被放弃后，AutoGen 0.7.5 的 runtime 在生成器体内继续驱动——
被"终止"的团队在后台继续烧 token、继续写黑板（僵尸）。修复后 _stream_team 的 finally
先 _runtime.stop()（丢弃排队轮次）再 aclose()；本文件用真实 SelectorGroupChat + 慢速
模型客户端在取消路径上验证：取消后 runtime 短时间内停止（排队轮次被丢弃）。"""

import asyncio

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from tripmate.blackboard import Blackboard
from tripmate.status import StatusBus
from tripmate.team import TeamRunner

_USAGE = RequestUsage(prompt_tokens=10, completion_tokens=10)


class _SlowBlockingClient(ReplayChatCompletionClient):
    """首轮 create_stream 长阻塞：把团队钉在"在途轮"上，供外部取消。"""

    def __init__(self):
        # 50 条存量：被急停丢弃的轮次不会再触发"响应耗尽"的内部异常（测试夹具噪声）
        super().__init__(chat_completions=["收到，继续。"] * 50)
        self.calls = 0

    async def create_stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(60)  # 模拟慢 LLM（测试会在其完成前取消）
        async for chunk in super().create_stream(messages, **kwargs):
            yield chunk
        yield CreateResult(finish_reason="stop", content="收到，继续。",
                           usage=_USAGE, cached=False)


def _build_team(model_client):
    a = AssistantAgent(name="WorkerA", model_client=model_client, system_message="测试工人A。")
    b = AssistantAgent(name="WorkerB", model_client=model_client, system_message="测试工人B。")
    return SelectorGroupChat([a, b], model_client=model_client, max_turns=3,
                             termination_condition=None,
                             selector_func=lambda msgs: "WorkerB")  # 确定性选人，不烧 replay 响应


def _runner():
    return TeamRunner(Blackboard(), StatusBus())


def test_cancelled_stream_team_stops_runtime_promptly():
    """取消 _stream_team 后：runtime 在短时间被 stop()（_run_context 置空），排队轮次被丢弃。

    修复前的行为：生成器被放弃，runtime 继续跑完 max_turns 轮才自停（僵尸）。"""
    client = _SlowBlockingClient()
    team = _build_team(client)
    runner = _runner()
    runner._usage_baseline = None

    async def main():
        task = asyncio.create_task(runner._stream_team(team, "测试任务"))
        await asyncio.sleep(0.5)  # 让团队进入首轮阻塞的 LLM 调用
        calls_before = client.calls
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 急停完成：_run_context 复位（生成器 finally 中的 join 被 immediate 清空解除）
        deadline = asyncio.get_running_loop().time() + 5.0
        while getattr(team._runtime, "_run_context", None) is not None:
            assert asyncio.get_running_loop().time() < deadline, "取消后 runtime 未及时停止"
            await asyncio.sleep(0.05)
        # 当前在途轮结束后不再有新调用（僵尸被杀）
        await asyncio.sleep(0.3)
        assert client.calls <= calls_before

    asyncio.run(main())


def test_abort_skips_stop_when_runtime_already_stopped():
    """正常完成路径：runtime 已自停（_run_context=None）时守卫跳过 stop，aclose 空操作。"""
    from tripmate.team import _abort_team_stream

    class FakeRT:
        def __init__(self):
            self._run_context = None
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1

    class FakeTeam:
        _runtime = FakeRT()

    consumed = {"closed": False}

    async def gen():
        try:
            yield "x"
        finally:
            consumed["closed"] = True

    g = gen()
    asyncio.run(g.__anext__())  # 挂起在 yield（未耗尽）
    asyncio.run(_abort_team_stream(FakeTeam(), g))
    assert FakeTeam._runtime.stop_calls == 0  # 守卫跳过
    assert consumed["closed"]                 # 生成器仍被释放


def test_abort_stops_running_runtime_and_closes_generator():
    """运行中急停：stop() 被调用、生成器被 aclose（GeneratorExit 注入）。"""
    from tripmate.team import _abort_team_stream

    class FakeRT:
        def __init__(self):
            self._run_context = object()
            self.stop_calls = 0

        async def stop(self):
            self.stop_calls += 1
            self._run_context = None

    class FakeTeam:
        _runtime = FakeRT()

    consumed = {"closed": False}
    wrote = {"n": 0}

    async def gen():
        try:
            while True:
                wrote["n"] += 1
                yield "zombie"
                await asyncio.sleep(0.05)
        finally:
            consumed["closed"] = True

    g = gen()
    asyncio.run(g.__anext__())
    asyncio.run(_abort_team_stream(FakeTeam(), g))
    assert FakeTeam._runtime.stop_calls == 1
    assert consumed["closed"]
