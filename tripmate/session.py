"""会话装配（MVP：单用户单会话，§2.3）：黑板 + 状态总线 + 聊天 Agent + 团队运行器。"""

from __future__ import annotations

import asyncio
import re

from .blackboard import Blackboard
from .chatter import build_chatter, ensure_travel_dates, stream_chatter
from .models import Draft, FinalDelivery
from .status import AUDIT, StatusBus
from .team import TeamRunner
from .config import ServerConfig

# 转述调用超时上限（秒）：免费 LLM 通道限流时 openai 客户端会静默重试很久，
# 不设上限会占死 chatter_lock，并曾异常冒泡烧掉网关推送协程（2026-08-29 诊断）。
RELAY_TIMEOUT_S = 180.0
# 用户消息处理超时（秒）：主备两客户端最坏路径 ~450s（主 150+备 300），480s 留余量；
# 低于最坏值会在拥堵窗口把"本可完成的回复"误杀成"处理超时请重发"。超时重建 Chatter 丢弃污染上下文。
CHAT_TIMEOUT_S = 480.0

_BARE_TOOL = re.compile(r"^(?:start_planning|submit_draft_feedback|get_travel_profile|save_travel_info|stop_planning)\b")
_TOOL_MARKUP = re.compile(r"<[/]?(?:tool_calls?|tool_sep|arg_key|arg_value|args)[^>]*>", re.IGNORECASE)
# 启动意图断言：模型可能宣布启动（中文）而未真正调用 start_planning 工具，
# 确定性兜底需覆盖中英文多种表述（"启动新一轮规划"为 2026-08-30 实测漏接变体；
# "现在开始为您规划"等带称呼语的插入变体为 2026-08-31 实测漏接变体——兜底未命中导致
# 团队从未启动，状态面板全程无事件）。负向断言排除"再/后/别/不/未/没/暂"等延迟拒绝语义；
# 声明跨度内禁跨句（！？），防止把下一句的打算误当成启动宣言。
_START_INTENT = re.compile(
    r"(?<![不别未勿再后没暂])(?:开始|启动)[^。！？]{0,8}规划"
    r"|规划团队[^。！？]{0,8}(?<![不别未勿再后没暂])启动"
    r"|start_planning")
# 反馈提交意图断言：草稿待反馈期模型可能宣布"已把修改意见转给团队"而不真正调用
# submit_draft_feedback（2026-08-31 完整流程实测两种变体："把这条修改意见转给规划团队"、
# "我来提交给规划团队"——宣布后团队闲置，修订流程卡死）。
_FEEDBACK_INTENT = re.compile(
    r"(?:转给|提交给|反馈给|转达给)(?:规划)?团队"
    r"|(?:修改意见|反馈|意见)[^。！？]{0,6}(?:已)?(?:提交|转达)(?:给)?(?:规划)?团队")


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

        兜底修复 1：provider 偶发把工具调用序列化成文本（工具未真正执行）——
        检测到裸工具名/工具标记时先重试一轮；start_planning 仍失败则确定性启动（§4.1 启动判定语义保持）。
        兜底修复 2：模型可能用中文宣布启动而不真正调工具（"启动新一轮规划"实测变体）——
        回复含启动意图且画像齐备时确定性补启动；**画像不齐时保留 Chatter 的追问原文**（信息不全时
        该回复本就是追问，绝不用启动失败的内部术语文案覆盖它，2026-08-30 用户体验事故）。
        """
        tools_seen: set[str] = set()  # 本轮真正执行过的工具名（判定"宣布启动但工具未执行"）
        async with self.chatter_lock:
            try:
                reply = await asyncio.wait_for(
                    stream_chatter(self.chatter, text, seen_tools=tools_seen), timeout=CHAT_TIMEOUT_S)
            except asyncio.TimeoutError:
                AUDIT.output("Chatter", f"用户消息处理超过 {int(CHAT_TIMEOUT_S)}s，重建 Chatter 实例丢弃污染上下文")
                self.chatter = build_chatter(self.bb, self.bus, self.runner)
                return "（系统提示）刚才的请求处理超时了，请把需求再发一次，我会重新处理。"
            if _missed_tool_call(reply):
                nudge = ("你上一条回复想调用的工具被序列化成了文字，没有真正执行。"
                         "请立即通过工具调用通道真正执行该工具，然后给用户一句简短的自然语言回复。")
                try:
                    reply2 = await asyncio.wait_for(
                        stream_chatter(self.chatter, nudge, source="system", seen_tools=tools_seen),
                        timeout=CHAT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    AUDIT.output("Chatter", "nudge 补试超时，重建 Chatter 丢弃污染上下文")
                    self.chatter = build_chatter(self.bb, self.bus, self.runner)
                    reply2 = ""
                if reply2 and not _missed_tool_call(reply2):
                    reply = reply2
                else:
                    # 两轮仍文本化：不再把乱码回给用户，诚实请其重发（黑板状态未动，草稿仍待反馈）
                    AUDIT.output("Chatter", "工具调用两轮文本化，降级为请用户重发")
                    reply = "（系统提示）我这条指令没有成功执行，请把刚才的话再发一次，我会重新处理。"
            if not (reply or "").strip():
                # 空回复治理（2026-08-30 用户反馈：话只进了审计日志、聊天页无输出）：补一轮总结
                AUDIT.output("Chatter", "回复为空，nudge 补一轮自然语言总结")
                try:
                    reply = await asyncio.wait_for(
                        stream_chatter(self.chatter,
                                       "请用一两句自然的中文告诉用户当前进展，以及接下来需要用户做什么。"
                                       "不要提任何内部术语。",
                                       source="system", seen_tools=tools_seen),
                        timeout=CHAT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    AUDIT.output("Chatter", "空回复总结超时，重建 Chatter 丢弃污染上下文")
                    self.chatter = build_chatter(self.bb, self.bus, self.runner)
                    reply = ""
                reply = (reply or "").strip() or "我正在处理您的请求，请稍候；如有需要我会随时与您确认。"
            # 工具本轮已真正执行过时跳过兜底：回执即启动确认，避免对成功路径二次干预/重复启动
            if reply and "start_planning" not in tools_seen and _START_INTENT.search(reply):
                missing = self.bb.profile.basic_info.missing_required()
                if missing:
                    # 信息不全：这条回复是礼貌追问而非启动宣言，原样放行，绝不覆盖
                    AUDIT.output("Chatter", f"回复含启动字样但缺 {'、'.join(missing)}，保留追问不兜底")
                else:
                    task = self.runner._task
                    if not self.runner.active and (task is None or task.done()):
                        await ensure_travel_dates(self.bb, self.bus)  # 与工具路径对齐：日期缺失确定性补齐
                        receipt = self.runner.start()
                        if receipt["status"] == "accepted":
                            reply = ("信息已齐备，旅行规划团队已在后台启动 🚀 "
                                     "规划期间您可以继续补充或修改信息，草稿出来后我会请您确认。")
                        else:
                            reply = "规划团队暂时忙碌，您可以继续补充信息，稍后再告诉我开始规划。"
            # 兜底修复 3：草稿待反馈期，模型宣布"已把修改意见转给团队"但未真正调用工具
            # （判闲与 submit_feedback 同源：_task 完成，而非 active 标志——collect 出草稿后
            # active 仍为 True；工具未执行 + 待反馈才触发；nudge 一轮强制真调，失败诚实请用户重发）
            _fb_task = self.runner._task
            if (reply and "submit_draft_feedback" not in tools_seen
                    and (_fb_task is None or _fb_task.done()) and self.runner._awaiting_feedback
                    and self.bb.profile.draft and _FEEDBACK_INTENT.search(reply)):
                AUDIT.output("Chatter", "回复宣称已提交修改意见但工具未执行，nudge 重试")
                try:
                    reply2 = await asyncio.wait_for(
                        stream_chatter(self.chatter,
                                       "你上一条回复声称已把修改意见转给规划团队，但没有真正调用 submit_draft_feedback "
                                       "工具，修改意见并未提交。请立即通过 submit_draft_feedback 工具真正提交：feedback "
                                       "取用户本轮消息里的修改意见原文、confirmed=false（用户明确说确认草稿才是 true），"
                                       "然后给用户一句简短的自然语言回复。",
                                       source="system", seen_tools=tools_seen),
                        timeout=CHAT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    AUDIT.output("Chatter", "反馈 nudge 超时，重建 Chatter 丢弃污染上下文")
                    self.chatter = build_chatter(self.bb, self.bus, self.runner)
                    reply2 = ""
                if reply2 and "submit_draft_feedback" in tools_seen:
                    reply = reply2
                else:
                    AUDIT.output("Chatter", "修改意见两轮未真正提交，降级为请用户重发")
                    reply = "（系统提示）刚才的修改意见没有成功提交，请把它再发一次，我会立即转给规划团队。"
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
            out = await asyncio.wait_for(_do(), timeout=RELAY_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — 转述失败必须降级，不能影响主推送链路
            AUDIT.output("Chatter", f"转述降级：{type(e).__name__}: {e}")
            # 超时取消会在 Chatter 上下文里留下无回应的转述请求，重建实例丢弃污染上下文
            self.chatter = build_chatter(self.bb, self.bus, self.runner)
            return "成果已通过界面卡片发送，可随时向我询问详情；本次语音转述暂时不可用。"
        # 空转述守卫（clean_reply 去掉非空 fallback 后可能返回空串，防前端空气泡）
        return out if (out or "").strip() else "成果已通过界面卡片发送，可随时向我询问详情。"

    def profile_snapshot(self) -> dict:
        data = self.bb.profile.model_dump(mode="json")
        data.pop("changelog", None)
        return data
