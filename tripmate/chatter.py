"""聊天 Agent（Chatter，§4.1）：系统唯一用户入口、团队之外常驻（闲置态只跑它，§3.2）。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date as _date

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import (BaseAgentEvent, BaseChatMessage,
                                        ToolCallExecutionEvent,
                                        ToolCallRequestEvent)

from . import prompts
from .blackboard import Blackboard
from .llm import get_model_client
from .models import TRAVEL_MODES
from .status import AUDIT, StatusBus
from .team import TeamRunner

# 默认值补齐规则（§2.1）：字段 → (默认值, 草稿标注文案)。
# date_text（出行时间段）不在静默默认之列——它是必问项（§4.1）：先追问一次，
# 用户明确表示"没定/近期"才写入；用户跳过追问时由 start_planning 兜底补"近期"并标注。
DEFAULTS: dict[str, tuple[object, str]] = {
    "travel_mode": ("高铁", "出行方式默认高铁"),
    "style": (["休闲"], "游玩风格默认休闲"),
    "party_size": (1, "同行人数默认 1 人"),
}

# 区域型目的地 → 代表性核心城市（仅精确匹配整词；宽泛词如"陕西"不自动替换，避免误判）
REGION_ALIAS: dict[str, str] = {"陕南": "汉中", "陕西南部": "汉中", "陕南地区": "汉中"}

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _pre_coerce_field(key: str, value):
    """LLM 脏输入的宽松转型（黑板 schema 校验前的第一道）：
    "3天"→3、"6000元"→6000.0、"6000万"→60000000.0、"休闲"→["休闲"]、"300-500"→[300.0,500.0]。"""
    if key in ("days", "party_size", "min_star") and isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            return int(float(m.group()))
    if key in ("budget", "budget_max") and isinstance(value, str):
        s = value.replace("，", "").replace(",", "").strip()
        m = _NUM_RE.search(s)
        if m:
            return float(m.group()) * (10000.0 if "万" in s else 1.0)
    if key in ("style", "must_visit", "food_restrictions") and isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if key == "travel_dates" and isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if key == "price_range" and isinstance(value, str):
        nums = [float(x) for x in _NUM_RE.findall(value)]
        if len(nums) >= 2:
            return nums[:2]
    return value


def _validate_updates(model, updates: dict) -> tuple[dict, list[str]]:
    """逐字段 schema 校验，返回（可写字段, 拒绝清单）——拒绝项随工具结果回传 LLM 自纠。"""
    from pydantic import ValidationError

    from .blackboard import coerced_field
    accepted, rejected = {}, []
    for key, val in updates.items():
        if not hasattr(model, key):
            rejected.append(f"{key}：不是可写字段")
            continue
        try:
            accepted[key] = coerced_field(model, key, val)
        except ValidationError as e:
            rejected.append(f"{key}：{_err_msg(e)}")
    return accepted, rejected


def _err_msg(e) -> str:
    try:
        return str(e.errors()[0].get("msg", "类型不符"))
    except Exception:  # noqa: BLE001
        return "类型不符"


def _reexpand_dates_after_days_change(basic_updates: dict, prof) -> None:
    """用户只改天数、未给新日期时，按既有出发日重展开逐日序列（就地写入 basic_updates）。

    否则天数与 travel_dates 口径漂移：酒店晚数/车票日期按旧序列查询，而日程按新天数排——
    且 days 变更现已正确触发 tickets/hotels/weather 重查（FIELD_IMPACT 补 days），旧序列会被
    当作"用户要的日期"再次使用。"""
    if "days" not in basic_updates or "travel_dates" in basic_updates:
        return
    new_days = basic_updates.get("days")
    if not isinstance(new_days, int) or new_days <= 0:
        return
    old_dates = prof.basic_info.travel_dates
    if not old_dates:
        return
    from .mocks.data import expand_dates
    try:
        basic_updates["travel_dates"] = expand_dates(old_dates[0], new_days)
    except (ValueError, TypeError):
        pass  # 非法既有日期交给后续流程容错


async def ensure_travel_dates(bb: Blackboard, bus: StatusBus) -> bool:
    """启动前兜底（问题 4）：出行时间段缺失时确定性写入默认"近期"并在草稿标注。

    正常路径由提示词驱动 Chatter 先追问；本函数防提示词失效导致无日期启动。
    返回是否做了补齐。"""
    basic = bb.profile.basic_info
    if basic.travel_dates or basic.date_text:
        return False
    await bb.apply_basic_info({"date_text": "近期"}, "chatter", "默认值补齐（出行时间默认近期）")
    await bb.apply_basic_info(
        {"defaults_applied": bb.profile.basic_info.defaults_applied + ["出行时间默认近期"]},
        "chatter", "记录默认值")
    await bus.emit("Chatter", "出行时间段未提供，按「近期」处理（已在草稿标注，可随时修改）", "STATUS_INFO")
    return True


def build_chatter(bb: Blackboard, bus: StatusBus, runner: TeamRunner,
                  memory_text: str = "") -> AssistantAgent:
    async def save_travel_info(basic_info: str, detail_info: str) -> str:
        """把本轮抽取到的旅行画像字段写入共享黑板（未提及的字段不要传）。
        basic_info：JSON 对象，可含 origin、destination、days、travel_mode、travel_dates、date_text、
                    style、budget、budget_max、party_size；
        detail_info：JSON 对象，可含 hotel{location_pref,price_range,min_star}、must_visit、
                    food_restrictions、pace、special_needs。两者可为空对象 {}。"""
        try:
            basic_updates = json.loads(basic_info) if basic_info and basic_info.strip() else {}
            detail_updates = json.loads(detail_info) if detail_info and detail_info.strip() else {}
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "error": f"JSON 解析失败：{e}"}, ensure_ascii=False)
        if not isinstance(basic_updates, dict) or not isinstance(detail_updates, dict):
            return json.dumps({"status": "error", "error": "参数必须是 JSON 对象"}, ensure_ascii=False)

        prof = bb.profile
        # 第一道：LLM 脏输入宽松转型（"3天"→3 等）；hotel 嵌套键同样处理
        basic_updates = {k: _pre_coerce_field(k, v) for k, v in basic_updates.items()}
        detail_updates = {
            k: ({hk: _pre_coerce_field(hk, hv) for hk, hv in v.items()}
                if k == "hotel" and isinstance(v, dict) else _pre_coerce_field(k, v))
            for k, v in detail_updates.items()
        }
        # budget 联动：budget 更新而 budget_max 是旧预算推导值时同步重算
        new_budget = basic_updates.get("budget")
        if new_budget and prof.basic_info.budget and prof.basic_info.budget_max:
            if abs(prof.basic_info.budget_max - prof.basic_info.budget * 1.2) < 0.01:
                basic_updates["budget_max"] = round(new_budget * 1.2, 1)

        # travel_dates 规范化：用户区间语义（[出发日, 返程日] 或单日起点）→ 完整逐日序列
        # 例：days=3 且 ["2026-10-01","2026-10-03"] → ["2026-10-01","2026-10-02","2026-10-03"]
        dates = basic_updates.get("travel_dates")
        days = basic_updates.get("days") or prof.basic_info.days
        if dates and isinstance(dates, list) and days:
            from .mocks.data import expand_dates
            try:
                if len(dates) == 1:
                    basic_updates["travel_dates"] = expand_dates(dates[0], days)
                elif len(dates) == 2 and dates[1] != dates[0]:
                    span = (_date.fromisoformat(dates[1]) - _date.fromisoformat(dates[0])).days + 1
                    if span == days:
                        basic_updates["travel_dates"] = expand_dates(dates[0], days)
            except ValueError:
                pass  # 非法日期格式交给后续流程容错
        _reexpand_dates_after_days_change(basic_updates, prof)

        # 区域型目的地解析（仅精确匹配 REGION_ALIAS 整词）：代表城市入 destination，区域表述留 special_needs
        dest = basic_updates.get("destination")
        if dest in REGION_ALIAS:
            resolved = REGION_ALIAS[dest]
            basic_updates["destination"] = resolved
            basic_updates["defaults_applied"] = list(bb.profile.basic_info.defaults_applied) \
                + [f"destination 按区域解析：{dest}→{resolved}"]
            await bus.emit("Chatter", f"目的地「{dest}」按区域解析为代表城市「{resolved}」（可随时在对话中修改）",
                           "STATUS_INFO")

        # 第二道：schema 校验——拒绝项随工具结果回传 LLM 自纠，可写字段才落黑板
        basic_ok, basic_rej = _validate_updates(bb.profile.basic_info, basic_updates)
        detail_ok, detail_rej = _validate_updates(bb.profile.detail_info, detail_updates)
        if basic_ok:
            await bb.apply_basic_info(basic_ok, "chatter", "用户输入抽取")
        if detail_ok:
            await bb.apply_detail_info(detail_ok, "chatter", "用户输入抽取")

        # 默认值补齐（仅当必填三要素齐备时；§2.1）
        basic = bb.profile.basic_info
        if not basic.missing_required():
            applied = []
            for key, (default, note) in DEFAULTS.items():
                cur = getattr(basic, key)
                if cur in (None, "", []) and default not in (None, "", []):
                    await bb.apply_basic_info({key: default}, "chatter", f"默认值补齐（{note}）")
                    applied.append(note)
            if basic.budget and not basic.budget_max:
                await bb.apply_basic_info({"budget_max": round(basic.budget * 1.2, 1)}, "chatter",
                                          "默认值补齐（最大预算=预算×1.2）")
                applied.append("最大预算默认 = 预算×1.2")
            if applied:
                old_defaults = [d for d in bb.profile.basic_info.defaults_applied]
                await bb.apply_basic_info({"defaults_applied": old_defaults + applied}, "chatter", "记录默认值")
                await bus.emit("Chatter", "默认值补齐：" + "；".join(applied) + "（将在草稿中标注）", "STATUS_INFO")
        # 用户后来给出真实值 → 从默认值标注中移除对应项（仅计真正落写的字段）
        updated_keys = set(basic_ok) | set(detail_ok)
        if updated_keys and bb.profile.basic_info.defaults_applied:
            remain = [d for d in bb.profile.basic_info.defaults_applied
                      if not any(k in d for k in updated_keys)]
            if remain != bb.profile.basic_info.defaults_applied:
                await bb.apply_basic_info({"defaults_applied": remain}, "chatter", "用户提供了真实值，移除默认标注")

        # 中途修改：团队闲置待反馈时触发检查点（运行中由阶段边界检查点处理，§5.3）
        runner.on_profile_changed()
        result = _profile_view(bb)
        rejected = basic_rej + detail_rej
        if rejected:
            data = json.loads(result)
            data["rejected_fields"] = rejected
            result = json.dumps(data, ensure_ascii=False)
        return result

    async def get_travel_profile() -> str:
        """读取共享黑板当前画像，用于转述与判定。返回中含 draft_summary（逐日行程+预算+预警）、
        guide_highlights（攻略景点/美食/避坑）与 final_summary（PDF+订单），可直接据此向用户转述。"""
        return _profile_view(bb)

    async def start_planning(with_images: bool = True) -> str:
        """启动旅行规划团队（Planning Team tool，§3.3）：传入共享黑板画像引用，返回受理回执。
        仅当必填三要素（出发地/目的地/天数）齐备且用户有明确规划意图时调用；
        出行时间段缺失时系统按"近期"兜底并在草稿标注。"""
        await ensure_travel_dates(bb, bus)
        receipt = runner.start(task="plan_trip", options={"with_images": with_images})
        if receipt["status"] == "accepted":
            await bus.emit("Chatter", "已启动旅行规划团队（后台运行，您可继续补充信息）", "STATUS_PHASE")
        return json.dumps(receipt, ensure_ascii=False)

    async def stop_planning() -> str:
        """停止当前正在运行的规划任务（用户说"停止/取消规划"时调用）。
        已收集的攻略/车票/酒店数据保留，停止后可继续补充信息并重新启动。"""
        receipt = runner.cancel(reason="用户通过聊天指示停止")
        return json.dumps(receipt, ensure_ascii=False)

    async def submit_draft_feedback(feedback: str, confirmed: bool) -> str:
        """提交草稿反馈。用户提出修改意见 → feedback=意见原文, confirmed=false；
        用户明确确认草稿 → confirmed=true（feedback 可为空）。"""
        receipt = await runner.submit_feedback(feedback=feedback, confirmed=confirmed)
        return json.dumps(receipt, ensure_ascii=False)

    agent = AssistantAgent(
        name="Chatter",
        model_client=get_model_client(),
        tools=[save_travel_info, get_travel_profile, start_planning, submit_draft_feedback,
               stop_planning],
        system_message=prompts.CHATTER_PROMPT + (memory_text or ""),
        reflect_on_tool_use=True,
    )
    return agent


def _profile_view(bb: Blackboard) -> str:
    """给 Chatter 的紧凑画像视图：基础画像 + 草稿摘要/攻略亮点/成品摘要（供转述与进度问答）。"""
    data = json.loads(bb.compact_json())
    p = bb.profile
    view = {
        "version": data["version"],
        "basic_info": data["basic_info"],
        "detail_info": data["detail_info"],
        "missing_required": p.basic_info.missing_required(),
        "has_draft": bool(data.get("draft")),
        "draft_feedback": data.get("draft_feedback"),
        "final": data.get("final"),
    }
    if p.draft is not None:
        from .planning import draft_summary_text
        view["draft_summary"] = draft_summary_text(p.draft)
        view["draft_budget"] = {"total": p.draft.budget_total, "warnings": p.draft.warnings[:3]}
    if p.final is not None:
        view["final_summary"] = {
            "pdf_url": p.final.pdf_url,
            "order_count": len(p.final.order_summary),
            "total_price": p.final.total_price,
        }
    if p.guide_digest:
        def _top(key: str) -> list[str]:
            # 保序去重（set 会打乱顺序，导致亮点展示不稳定）
            seen: dict[str, None] = {}
            for g in p.guide_digest:
                for x in getattr(g, key):
                    if x and x not in seen:
                        seen[x] = None
            return list(seen)[:3]
        view["guide_highlights"] = {"spots": _top("spots"), "foods": _top("foods"),
                                    "warnings": _top("warnings")}
    return json.dumps(view, ensure_ascii=False)


_TOOL_MARKUP = re.compile(r"<[/]?(?:tool_calls?|tool_sep|arg_key|arg_value|args|function|parameter)[^>]*>", re.IGNORECASE)
_TOOL_BLOCK = re.compile(r"<tool_calls?:[^>]*>.*?</tool_calls?>", re.IGNORECASE | re.DOTALL)


def clean_reply(text: str, fallback: str = "") -> str:
    """清洗模型偶发的工具调用标记泄漏（<tool_calls:...> 文本化输出）。"""
    cleaned = _TOOL_BLOCK.sub(" ", text or "")
    cleaned = _TOOL_MARKUP.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or fallback


async def stream_chatter(chatter: AssistantAgent, user_text: str, source: str = "user",
                         seen_tools: set[str] | None = None) -> str:
    """运行 Chatter 并把 Thought/Action/Observation 写入审计日志，返回最终回复文本。

    seen_tools：可选集合，收集本轮真正执行过的工具名（供"宣布启动但工具未执行"兜底判定）。
    finally aclose（修复 #1）：取消/超时路径上及时关闭 run_stream 生成器（单 Agent 无群聊
    runtime，aclose 即取消在途 HTTP 流），不留僵尸 LLM 调用。"""
    from autogen_agentchat.messages import TextMessage, ThoughtEvent
    last = ""
    stream = chatter.run_stream(task=TextMessage(content=user_text, source=source))
    try:
        while True:
            try:
                msg = await stream.__anext__()
            except StopAsyncIteration:
                break
            if isinstance(msg, ThoughtEvent):
                AUDIT.thought("Chatter", msg.content)
            elif isinstance(msg, ToolCallRequestEvent):
                for call in msg.content:
                    if getattr(call, "name", None):
                        AUDIT.action("Chatter", call.name, str(getattr(call, "arguments", "")))
            elif isinstance(msg, ToolCallExecutionEvent):
                if seen_tools is not None:
                    for c in msg.content:
                        if getattr(c, "name", None):
                            seen_tools.add(c.name)
                obs = "; ".join(getattr(c, "content", "") or "" for c in msg.content)[:400]
                AUDIT.observation("Chatter", obs)
            elif isinstance(msg, BaseChatMessage) and not isinstance(msg, BaseAgentEvent):
                if msg.source != "user":
                    # 不给非空 fallback：清洗后为空就该走 session 的空回复 nudge，
                    # 用"已处理您的消息。"搪塞只会掩蔽问题、让兜底链失效
                    last = clean_reply(msg.to_text())
                    AUDIT.output("Chatter", last)
    finally:
        try:
            await asyncio.wait_for(stream.aclose(), timeout=15.0)
        except Exception:  # noqa: BLE001 — 清理失败不影响主流程（异常生成器由 GC 兜底）
            AUDIT.observation("Chatter", "chatter run_stream aclose 失败/超时，已抛弃")
    return last
