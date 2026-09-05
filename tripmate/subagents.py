"""阶段三（方案二落地）：JobBoard 后台任务升级为 subagent run——orchestrator→workers 层级。

编排关系：
  群聊父 Agent（Researcher / BookingButler）--start_* 提交--> JobBoard 任务
    --> 每个任务由一个独立 subagent（AssistantAgent 实例）执行：
        单工具（无参，数据来自共享黑板）、max_turns=2、reflect_on_tool_use=False。
  父 Agent 收割（collect_*）写入黑板——父 Agent 工具语义/状态机/护栏零改动。

设计约束：
- 结果从 TaskResult 的 ToolCallExecutionEvent 提取（确定性，绝不解析自然语言）；
- subagent 未调工具/产出非 dict → 重试 1 次 → 降级为直接调用查询函数（无 LLM）——
  上层 mock 兜底链不变；subagent 的 LLM 调用计入全局用量（per-run 基线熔断照常生效）；
- 跨父 Agent 的 IO 重叠保留：JobBoard 两段式（start 提交 / collect 收割）不动，
  subagent 在后台任务体内并发运行。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import ToolCallExecutionEvent

from .llm import get_model_client
from .prompts import SUBAGENT_PROMPT
from .status import AUDIT

logger = logging.getLogger("tripmate.subagents")

_MAX_ATTEMPTS = 2      # subagent 未产出工具调用时的重试上限（之后降级直调）
_EXPECT_TYPE = dict    # 各查询通道均返回 dict；其他形态视为"未产出"


def _extract_tool_output(result: Any) -> Any | None:
    """从 TaskResult 逆序找 ToolCallExecutionEvent，解析工具结构化产出；无则 None。"""
    for msg in reversed(getattr(result, "messages", [])):
        if isinstance(msg, ToolCallExecutionEvent):
            for c in msg.content:
                content = getattr(c, "content", "")
                if isinstance(content, str):
                    try:
                        return json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        return None   # 工具报错文本（autogen 把异常转为错误字符串）
                return content
    return None


async def run_channel_subagent(channel: str, instruction: str,
                               query: Callable[[], Awaitable[Any]],
                               model_client=None,
                               notify: Callable[[str], Awaitable[None]] | None = None) -> Any:
    """以独立 subagent 执行一个查询通道；产出从工具事件提取，失败降级直调。

    channel：审计/日志标识（sub:guides 等）；instruction：任务描述（说明工具无参、
    调用即完成）；query：无参协程——同一函数既作为 subagent 的工具体，也作为降级
    直调路径（上层 mock 兜底不变）。
    notify：可选状态回调（"running"/"done"/"failed"），驱动前端 subagent 指示灯
    （黄=运行中/绿=完成/红=失败）；回调异常绝不影响查询主流程、绝不触发重试。
    """
    client = model_client if model_client is not None else get_model_client()

    async def _notify(state: str) -> None:
        if notify is None:
            return
        try:
            await notify(state)
        except Exception as e:  # noqa: BLE001 — 状态推送失败不影响查询
            logger.warning("subagent 状态回调失败（%s/%s）：%s: %s", channel, state,
                           type(e).__name__, e)

    async def query_channel_data() -> str:
        data = await query()
        return json.dumps(data, ensure_ascii=False, default=str)

    await _notify("running")
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            agent = AssistantAgent(
                name=f"{channel}_worker",
                model_client=client,
                tools=[query_channel_data],
                system_message=SUBAGENT_PROMPT.format(channel=channel),
                reflect_on_tool_use=False,   # 工具结果直接回传，省一轮反思 LLM 调用
            )
            result = await agent.run(task=instruction)
            data = _extract_tool_output(result)
            if isinstance(data, _EXPECT_TYPE):
                AUDIT.observation(f"sub:{channel}", f"subagent 产出 {len(data)} 键（attempt={attempt}）")
                await _notify("done")
                return data
            AUDIT.observation(f"sub:{channel}", f"subagent 未产出工具结果（attempt={attempt}）")
        except Exception as e:  # noqa: BLE001 — 单通道 subagent 失败走降级，不拖垮任务
            AUDIT.observation(f"sub:{channel}", f"subagent 运行异常（attempt={attempt}）：{type(e).__name__}: {e}")
    AUDIT.observation(f"sub:{channel}", "subagent 两轮未产出，降级为直接调用查询函数")
    try:
        out = await query()
    except Exception:
        await _notify("failed")
        raise
    await _notify("done")
    return out
