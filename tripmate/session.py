"""会话装配（MVP：单用户单会话，§2.3）：黑板 + 状态总线 + 聊天 Agent + 团队运行器。"""

from __future__ import annotations

import asyncio
import re
from collections import deque

from .blackboard import Blackboard
from .chatter import build_chatter, ensure_travel_dates, stream_chatter
from .models import Draft, FinalDelivery, TravelProfile
from .persistence import load_session, save_session
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
_TOOL_MARKUP = re.compile(r"<[/]?(?:tool_calls?|tool_sep|arg_key|arg_value|args|function|parameter)[^>]*>", re.IGNORECASE)
# 启动意图断言：模型可能宣布启动（中文）而未真正调用 start_planning 工具，
# 确定性兜底需覆盖中英文多种表述（"启动新一轮规划"为 2026-08-30 实测漏接变体；
# "现在开始为您规划"等带称呼语的插入变体为 2026-08-31 实测漏接变体；
# "马上让规划团队开工"的"开工"措辞为 2026-09-01 实测漏接变体；"让规划团队开始
# 为您安排"的"开始"措辞为 2026-09-06 e2e 实测漏接变体（模型宣称启动却未调工具，
# 兜底未命中导致团队从未启动、总线静默）——兜底未命中导致
# 团队从未启动，状态面板全程无事件）。负向断言排除"再/后/别/不/未/没/暂"等延迟拒绝语义；
# 声明跨度内禁跨句（！？），防止把下一句的打算误当成启动宣言。
_START_INTENT = re.compile(
    r"(?<![不别未勿再后没暂])(?:开始|启动)[^。！？]{0,8}规划"
    r"|规划团队[^。！？]{0,8}(?<![不别未勿再后没暂])(?:启动|开工|开始)"
    r"|start_planning")
# 反馈提交意图断言：草稿待反馈期模型可能宣布"已把修改意见转给团队"而不真正调用
# submit_draft_feedback（2026-08-31 完整流程实测两种变体："把这条修改意见转给规划团队"、
# "我来提交给规划团队"——宣布后团队闲置，修订流程卡死）。
# 2026-09-05 e2e 实测第三种变体：模型把调用文本化为 "、submit_draft_feedback(confirm=false,...)"
# （前导顿号 + 函数调用形态）——加入裸工具名分支，使专用反馈 nudge 接管。
# 2026-09-06 e2e 实测第四种变体：用户消息同时含可抽取字段与草稿修改意见（"第 2 天换成都江堰"），
# 模型只调 save_travel_info（信息更新）而漏掉 submit_draft_feedback——反馈丢失、修订流程从未启动。
# 增加用户原文侧的草稿修改意图断言（_USER_MODIFY_INTENT）作为补触发判据。
_FEEDBACK_INTENT = re.compile(
    r"(?:转给|提交给|反馈给|转达给)(?:规划)?团队"
    r"|(?:修改意见|反馈|意见)[^。！？]{0,6}(?:已)?(?:提交|转达)(?:给)?(?:规划)?团队"
    r"|submit_draft_feedback\b")
_USER_MODIFY_INTENT = re.compile(
    r"第\s*[一二两三四五六七八九十\d]+\s*天[^。！？]{0,14}"
    r"(?:换成|换到|改为|改成|替换|加上|加个|增加|去掉|删掉|删除|移除|提前|延后|不去)"
    r"|(?:把|将)[^。！？]{0,14}(?:换成|换到|改为|改成|替换|去掉|删掉|删除|移除)"
    r"|(?:换成|改为|改成|替换成|加上|加个|增加)[^。！？]{0,12}"
    r"(?:景点|酒店|餐厅|民宿|车次|航班|安排|行程)"
    r"|(?:去掉|删掉|删除|移除|不要去|不去)"
    r"|(?:行程|草稿|安排|路线)[^。！？]{0,6}(?:调整|修改|改一下|换一下|重新排)")


def _missed_tool_call(reply: str) -> bool:
    """检测 provider 把工具调用序列化成文本的失败模式（裸工具名或工具标记泄漏）。

    2026-09-05 e2e 实测变体：前导标点（"、"等）后跟裸工具名/函数调用形态——
    先剥离前导标点再匹配，否则 "、submit_draft_feedback(...)" 永远漏检，
    反馈永不真正提交、修订流程卡死。"""
    t = (reply or "").strip()
    t = re.sub(r"^[、，,。．.\s:：;；!！?？]+", "", t)
    return bool(_BARE_TOOL.match(t) or _TOOL_MARKUP.search(t))


class Session:
    # 类级默认：部分测试以 Session.__new__ 绕过 __init__ 构造，新属性必须有类级兜底
    username: str = ""
    memory_dirty: bool = False
    sub_states: dict | None = None   # None 仅出现在 __new__ 构造的测试实例；网关路径走 __init__

    def __init__(self, sid: str = "", username: str = "") -> None:
        self.sid = sid
        self.username = username
        # subagent 指示灯真值（channel → running/done/failed）：前端刷新/重连后由
        # 网关快照补发恢复——STATUS_REPLAY=80 的滚动窗口会被心跳挤出，灯态必须单独记
        self.sub_states: dict[str, str] = {}
        self.bb = Blackboard()
        self.bus = StatusBus(replay_limit=ServerConfig.STATUS_REPLAY)
        # 团队完成事件队列：草稿就绪 / 定稿完成 / 错误（由后台任务投递，WS 发送协程消费）
        self.team_events: asyncio.Queue = asyncio.Queue()
        self.runner = TeamRunner(
            self.bb, self.bus, username=username,
            on_draft_ready=lambda d: self.team_events.put_nowait(("draft_ready", d)),
            on_completed=lambda f: self.team_events.put_nowait(("completed", f)),
            on_error=lambda e: self.team_events.put_nowait(("error", e)),
        )
        self.chatter = build_chatter(self.bb, self.bus, self.runner)
        self.chatter_lock = asyncio.Lock()
        # 当前正在处理的用户消息任务（stop 按钮联动中断）
        self.current_chat_task: asyncio.Task | None = None
        # 异步落盘单飞状态：_dirty=在途落盘期间又有新写入（尾随补写）
        self._flush_task: asyncio.Task | None = None
        self._dirty = False
        # 聊天历史（2026-09-03 断线丢回复根治）：创建即记录，与发送成败无关；
        # 断线重连时网关全量补播（此前只补状态时间线，断线窗口里的回复用户零收到）。
        self._chat_history: deque[dict] = deque(maxlen=200)
        # 会话持久化（2026-09-05）：有 sid 才启用（无 sid 的测试构造不落盘）。
        # 恢复聊天历史 + 画像快照（changelog 置空、版本号保留）——服务重启后
        # 刷新页面聊天记录与草稿/成品卡片经网关补播重现。
        # 偏好记忆置脏标记（2026-09-12 多用户版）：定稿提炼完成后锁内懒重建 Chatter
        self.memory_dirty = False
        if sid:
            self._restore()
            self.bb.on_change = self._persist_profile

    def _restore(self) -> None:
        data = load_session(self.sid, self.username)
        if not data:
            return
        for m in data.get("chat_history", [])[:200]:
            if isinstance(m, dict):
                self._chat_history.append(dict(m))
        profile_data = data.get("profile")
        if isinstance(profile_data, dict):
            try:
                self.bb.restore_profile(TravelProfile.model_validate(profile_data))
            except Exception as e:  # noqa: BLE001 — 恢复失败按全新会话处理（不阻塞启动）
                AUDIT.output("Persistence", f"会话画像恢复失败（{type(e).__name__}: {e}），按全新会话处理")

    def _schedule_flush(self) -> None:
        """单飞合并落盘（2026-09-05 去阻塞）：一次写盘在途时只置脏标记，写完尾随补一次。

        此前每次黑板写/每条聊天都在事件循环线程上同步全量序列化+写盘（Windows 单次可达
        几十毫秒），规划期间几十次写叠加会卡住所有会话的 WS 推送与 LLM 流式转发。"""
        if not self.sid:
            return
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._dirty = False
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_once())

    async def _write_once(self) -> None:
        """快照在事件循环线程上取（跨线程序列化会撞并发修改），json 序列化+写盘进线程。"""
        profile = self.bb.profile.model_dump(mode="json")
        profile.pop("changelog", None)  # 重启后检查点基线重置，旧条目无用且防文件无限膨胀
        chats = list(self._chat_history)
        await asyncio.to_thread(save_session, self.sid, chats, profile, self.username)

    async def _flush_once(self) -> None:
        try:
            while True:
                self._dirty = False
                await self._write_once()
                if not self._dirty:
                    break
        except Exception as e:  # noqa: BLE001 — 落盘失败绝不影响聊天/规划主流程
            AUDIT.output("Persistence", f"会话状态异步落盘失败（{type(e).__name__}: {e}）")
        finally:
            self._flush_task = None
        # 收尾与置脏竞争的兜底：刚清完任务标记的瞬间又置脏 → 再补一轮
        if self._dirty and self._flush_task is None:
            self._schedule_flush()

    async def flush(self) -> None:
        """等待在途落盘完成并立即补写尾随变更（停机钩子与测试断言用）。"""
        if not self.sid:
            return
        while self._flush_task is not None and not self._flush_task.done():
            try:
                await self._flush_task
            except Exception:  # noqa: BLE001
                pass
        self._dirty = False
        await self._write_once()

    def _persist_profile(self) -> None:
        """黑板 on_change 钩子：任何写入后调度落盘（自身异常已在钩子层兜底）。"""
        self._schedule_flush()

    def record_chat(self, payload: dict) -> None:
        """记录一条聊天消息（创建点调用，恰好一次；不含状态时间线事件）。"""
        self._chat_history.append(dict(payload))
        self._schedule_flush()

    def chat_history(self) -> list[dict]:
        return list(self._chat_history)

    def rebuild_chatter(self) -> None:
        """重建 Chatter 实例：超时/异常/被取消后丢弃被污染的对话上下文（与既有路径同源）；
        顺带带上最新偏好记忆文本（记忆更新后的热重建也走此处）。"""
        from .memory import prefs_prompt_text
        self.chatter = build_chatter(self.bb, self.bus, self.runner,
                                     memory_text=prefs_prompt_text(self.username))

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
            if self.memory_dirty:
                # 偏好记忆已更新：锁内热重建（绝不处理中换实例），下条消息即带最新偏好
                self.memory_dirty = False
                self.rebuild_chatter()
            try:
                reply = await asyncio.wait_for(
                    stream_chatter(self.chatter, text, seen_tools=tools_seen), timeout=CHAT_TIMEOUT_S)
            except asyncio.TimeoutError:
                AUDIT.output("Chatter", f"用户消息处理超过 {int(CHAT_TIMEOUT_S)}s，重建 Chatter 实例丢弃污染上下文")
                self.chatter = build_chatter(self.bb, self.bus, self.runner)
                return "（系统提示）刚才的请求处理超时了，请把需求再发一次，我会重新处理。"
            except Exception as e:  # noqa: BLE001 — 非超时 LLM 失败（如主备双通道连接错误，2026-09-04 实测）：
                # 与超时同源处理：重建实例丢弃污染上下文（无回应的用户消息残留在对话里）
                AUDIT.output("Chatter", f"用户消息处理失败（{type(e).__name__}: {e}），重建 Chatter 实例丢弃污染上下文")
                self.chatter = build_chatter(self.bb, self.bus, self.runner)
                return "（系统提示）刚才的请求没有处理成功，请把需求再发一次，我会重新处理。"
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
            # 兜底修复 4（2026-09-06 e2e 实测）：用户消息同时含可抽取字段与草稿修改意见时，
            # 模型只做信息更新（save_travel_info）漏掉 submit_draft_feedback——按用户原文的
            # 修改意图补触发。（判闲与 submit_feedback 同源：_task 完成；nudge 一轮强制真调，
            # 失败诚实请用户重发）
            _fb_task = self.runner._task
            _fb_announced = bool(_FEEDBACK_INTENT.search(reply or ""))
            _fb_user_intent = bool(_USER_MODIFY_INTENT.search(text or ""))
            if (reply and "submit_draft_feedback" not in tools_seen
                    and (_fb_task is None or _fb_task.done()) and self.runner._awaiting_feedback
                    and self.bb.profile.draft and (_fb_announced or _fb_user_intent)):
                AUDIT.output("Chatter", "回复宣称已提交修改意见但工具未执行，nudge 重试"
                             if _fb_announced else
                             "用户消息含草稿修改意图但模型只做了信息更新，nudge 补提交")
                nudge = ("你上一条回复声称已把修改意见转给规划团队，但没有真正调用 submit_draft_feedback "
                         "工具，修改意见并未提交。请立即通过 submit_draft_feedback 工具真正提交：feedback "
                         "取用户本轮消息里的修改意见原文、confirmed=false（用户明确说确认草稿才是 true），"
                         "然后给用户一句简短的自然语言回复。"
                         if _fb_announced else
                         "用户本轮消息是对当前草稿的修改意见（原文：\"" + text + "\"），你只更新了画像信息，"
                         "修改意见并未提交给规划团队，草稿仍停留在待确认状态。请立即调用 submit_draft_feedback "
                         "工具真正提交：feedback 取该条修改意见原文、confirmed=false（用户明确说确认草稿才是 "
                         "true），然后给用户一句简短的自然语言回复。")
                try:
                    reply2 = await asyncio.wait_for(
                        stream_chatter(self.chatter, nudge,
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
        except asyncio.CancelledError:
            # 转述被取消（如重连时网关取消旧 sender）：与超时同源——CancelledError 会留下
            # 无回应的转述请求污染 Chatter 上下文，重建实例后透传取消（2026-09-04 加固）
            self.chatter = build_chatter(self.bb, self.bus, self.runner)
            raise
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
