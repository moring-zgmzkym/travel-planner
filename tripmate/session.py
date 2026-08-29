"""会话装配（MVP：单用户单会话，§2.3）：黑板 + 状态总线 + 聊天 Agent + 团队运行器。"""

from __future__ import annotations

import asyncio
import re

from .blackboard import Blackboard
from .chatter import build_chatter, stream_chatter
from .models import Draft, FinalDelivery
from .status import AUDIT, StatusBus
from .team import TeamRunner
from .config import ServerConfig

# 转述调用超时上限（秒）：免费 LLM 通道限流时 openai 客户端会静默重试很久，
# 不设上限会占死 chatter_lock，并曾异常冒泡烧掉网关推送协程（2026-08-29 诊断）。
RELAY_TIMEOUT_S = 180.0

_BARE_TOOL = re.compile(r"^(?:start_planning|submit_draft_feedback|get_travel_profile|save_travel_info|stop_planning)\b")
_TOOL_MARKUP = re.compile(r"<[/]?(?:tool_calls?|tool_sep|arg_key|arg_value|args)[^>]*>", re.IGNORECASE)


def _missed_tool_call(reply: str) -> bool:
    """检测 provider 把工具调用文本化的失败模式（裸工具名或工具标记泄漏）。"""
    t = (reply or "").strip()
    return bool(_BARE_TOOL.match(t) or _TOOL_MARKUP.search(t))


class Session:
    def __init__(self) -> None:
        self.bb = Blackboard()
        self.bus = StatusBus(replay_limit=ServerConfig.STATUS_REPLAY)
        # 团队完成事件队列：草稿就绪 / 定稿完成 / 错误（由后台任务投递，WS 发送协程消费）
        self.team_events: asyncio.Queue = asyncio.Queue()
        self.runner = TeamRunner(
            self.bb, self.bus,
            on_draft_ready=lambda d: self.team_events.put_nowait(("draft_ready", d)),
            on_completed=lambda f: self.team_events.put_nowait(("completed", f)),
            on_error=lambda e: self.team_events.put_nowait(("error", e)),
        )
        self.chatter = build_chatter(self.bb, self.bus, self.runner)
        self.chatter_lock = asyncio.Lock()

    async def handle_user_message(self, text: str) -> str:
        """用户消息 → 聊天 Agent（串行化：同一时刻仅一次 Chatter 运行）。

        兜底修复：provider 偶发把工具调用序列化成文本（工具未真正执行）——
        检测到裸工具名/工具标记时先重试一轮；start_planning 仍失败则确定性启动（§4.1 启动判定语义保持）。
        """
        async with self.chatter_lock:
            reply = await stream_chatter(self.chatter, text)
            if _missed_tool_call(reply):
                nudge = ("你上一条回复想调用的工具被序列化成了文字，没有真正执行。"
                         "请立即通过工具调用通道真正执行该工具，然后给用户一句简短的自然语言回复。")
                reply2 = await stream_chatter(self.chatter, nudge, source="system")
                if reply2 and not _missed_tool_call(reply2):
                    reply = reply2
                else:
                    # 两轮仍文本化：不再把乱码回给用户，诚实请其重发（黑板状态未动，草稿仍待反馈）
                    AUDIT.output("Chatter", "工具调用两轮文本化，降级为请用户重发")
                    reply = "（系统提示）我这条指令没有成功执行，请把刚才的话再发一次，我会重新处理。"
            if reply and "start_planning" in reply:
                task = self.runner._task
                if not self.runner.active and (task is None or task.done()):
                    receipt = self.runner.start()
                    if receipt["status"] == "accepted":
                        reply = ("信息已齐备，旅行规划团队已在后台启动 🚀 "
                                 "规划期间您可以继续补充或修改信息，草稿出来后我会请您确认。")
                    else:
                        reply = f"启动规划未成功：{receipt.get('reason', '')}"
            return reply

    async def relay_team_event(self, note: str) -> str:
        """团队事件 → 聊天 Agent 转述（§3.8 完成回传）。

        转述是增强路径：失败必须降级为固定文案，绝不向上抛异常——
        此前内联调用遇 LLM 限流（429 长重试）异常冒泡，烧掉网关 _sender 推送协程，
        导致草稿卡片之后的所有成果（含 PDF）到不了前端。锁放在 wait_for 内层，
        超时后 CancelledError 穿透 async with 自动释放 chatter_lock。
        """
        async def _do() -> str:
            async with self.chatter_lock:
                return await stream_chatter(self.chatter, f"[系统提示·请转述给用户] {note}", source="system")

        try:
            return await asyncio.wait_for(_do(), timeout=RELAY_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — 转述失败必须降级，不能影响主推送链路
            AUDIT.output("Chatter", f"转述降级：{type(e).__name__}: {e}")
            # 超时取消会在 Chatter 上下文里留下无回应的转述请求，重建实例丢弃污染上下文
            self.chatter = build_chatter(self.bb, self.bus, self.runner)
            return "成果已通过界面卡片发送，可随时向我询问详情；本次语音转述暂时不可用。"

    def profile_snapshot(self) -> dict:
        data = self.bb.profile.model_dump(mode="json")
        data.pop("changelog", None)
        return data
