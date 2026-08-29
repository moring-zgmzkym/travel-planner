"""旅行规划团队（企划书 §3/§4/§5）：

- 四 Agent 对等协同：SelectorGroupChat + 确定性 selector 状态机（"会议主持人"只决定谁发言，§3.4）
- 真并行收集：JobBoard 后台任务，攻略搜索与车票/酒店查询时间戳交叉（验收 #5）
- 阶段化运行 + 检查点 + 变更影响分析增量重跑（§3.8/§5.3）
- Team 封装为 Tool 的契约入口 TeamRunner.start()（§3.3）
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import (BaseAgentEvent, BaseChatMessage,
                                         ToolCallExecutionEvent,
                                         ToolCallRequestEvent)
from autogen_agentchat.teams import SelectorGroupChat

from . import prompts
from .blackboard import Blackboard
from .config import BudgetConfig
from .llm import TokenBudgetExceeded, check_budget, get_model_client
from .mocks.data import kb_for_city
from .models import (Draft, DraftDay, DraftFeedback, FinalDelivery, GuideDigestItem,
                     HotelCandidate, ImageItem, PlanInput, TicketCandidate, TravelProfile)
from .pdf_gen import build_pdf
from .planning import PACE_SPOTS, analyze_impact, compute_budget, validate_draft
from .status import AUDIT, StatusBus
from .tools.hotels import query_hotels, score_and_select as score_hotels
from .tools.search import search_guides, search_images
from .tools.tickets import query_tickets, score_and_select as score_tickets
from .tools.weather import query_weather

AGENT_PROC = "InformationProcessor"
AGENT_RES = "Researcher"
AGENT_MCP = "BookingButler"
AGENT_PLANNER = "Planner"

# 状态机步骤 → 发言人（对等协议，§6.2 消息类型表）
SPEAKER: dict[str, str] = {
    "PROC_BROADCAST": AGENT_PROC,
    "RES_START": AGENT_RES,
    "MCP_START": AGENT_MCP,
    "RES_COLLECT": AGENT_RES,
    "MCP_COLLECT": AGENT_MCP,
    "PROC_SUMMARIZE": AGENT_PROC,
    "PLAN_DRAFT": AGENT_PLANNER,
    "PLAN_IMGREQ": AGENT_PLANNER,
    "RES_IMG": AGENT_RES,
    "PLAN_PDF": AGENT_PLANNER,
    "DONE": AGENT_PROC,
}
INITIAL_STEP = {"collect": "PROC_BROADCAST", "revise": "PLAN_DRAFT", "finalize": "PLAN_IMGREQ"}

MARKER_DONE = "DRAFT_READY"   # collect/revise 阶段完成标记
MARKER_FINAL = "FINAL_PDF"    # finalize 阶段完成标记


class SectionReadyTermination:
    """黑板目标分区在本阶段内被（重新）写入即终止（§3.6 黑板为唯一事实源；分区由工具或护栏写入）。

    以分区版本号判断：阶段开始时记录基线，运行中分区版本超过基线即认为阶段目标达成——
    这保证 revise/增量重跑阶段不会因旧草稿已存在而误终止。
    """

    def __init__(self, bb: Blackboard, section: str, label: str, baseline_version: int) -> None:
        self._bb = bb
        self._section = section
        self._label = label
        self._baseline = baseline_version
        self._terminated = False

    @property
    def terminated(self) -> bool:
        return self._terminated

    async def __call__(self, messages) -> object | None:
        from autogen_agentchat.messages import StopMessage
        if self._terminated:
            from autogen_agentchat.base import TerminatedException
            raise TerminatedException("Termination condition has already been reached")
        if self._bb.section_version(self._section) > self._baseline:
            self._terminated = True
            return StopMessage(content=f"{self._label} 已写入共享黑板，阶段完成",
                               source="SectionReadyTermination")
        return None

    async def reset(self) -> None:
        self._terminated = False


@dataclass
class TeamState:
    """selector 状态机（工具推进步骤；selector 只做 步骤→发言人 映射）。"""

    phase: str = "collect"
    step: str = INITIAL_STEP["collect"]
    last_speaker: str = ""
    consecutive: int = 0


class JobBoard:
    """并行收集任务板：start_* 工具提交后台任务，collect_* 工具收割（真并行，验收 #5）。"""

    def __init__(self) -> None:
        self._jobs: dict[str, asyncio.Task] = {}

    def submit(self, key: str, coro) -> None:
        if key in self._jobs and not self._jobs[key].done():
            self._jobs[key].cancel()
        self._jobs[key] = asyncio.create_task(coro)

    def has(self, key: str) -> bool:
        return key in self._jobs

    async def collect(self, key: str) -> Any:
        if key not in self._jobs:
            raise RuntimeError(f"任务 {key} 尚未提交")
        return await asyncio.shield(self._jobs[key])

    def clear(self) -> None:
        for t in self._jobs.values():
            if not t.done():
                t.cancel()
        self._jobs.clear()


@dataclass
class TeamContext:
    """工具闭包共享的上下文。"""

    bb: Blackboard
    bus: StatusBus
    state: TeamState
    jobs: JobBoard
    runner: "TeamRunner"
    run_id: str
    # 变更影响分析给出的通道复用开关（增量重跑：True=复用缓存不重查）
    reuse: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具集（各 Agent 的 Action；Observation 为返回字符串，写入黑板分区）
# ---------------------------------------------------------------------------

def _ok(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def make_researcher_tools(ctx: TeamContext):
    """信息收集 Agent：攻略搜索（start/collect/write 三段式）+ 图片搜索。"""

    async def _guide_job() -> dict:
        prof: TravelProfile = ctx.bb.profile
        dest = prof.basic_info.destination or ""
        month = prof.basic_info.date_text or ""
        await ctx.bus.emit(AGENT_RES, f"攻略搜索中…（{dest}｜小红书 / 马蜂窝 / 百度 三路并行）", "STATUS_COLLECT")
        r = await search_guides(dest, month)
        tag = "降级参考值" if r["mode"] == "mock" else "实时"
        await ctx.bus.emit(AGENT_RES, f"攻略搜索完成：{len(r['digest'])} 份来源（{tag}）", "STATUS_COLLECT")
        return r

    async def start_guide_search() -> str:
        """启动攻略搜索任务（小红书/马蜂窝/百度三路并行）。无参数：画像从共享黑板读取。
        必须真正调用本工具（不允许只回复文字），工具返回后再简短回复 SEARCH_STARTED。"""
        prof: TravelProfile = ctx.bb.profile
        dest = prof.basic_info.destination or ""
        if ctx.reuse.get("guides") and prof.guide_digest:
            ctx.state.step = "MCP_START"
            AUDIT.thought(AGENT_RES, f"变更影响分析：攻略复用缓存（{len(prof.guide_digest)} 份来源），不重搜")
            return _ok(status="reused", note="攻略复用缓存（未受变更影响）", sources=len(prof.guide_digest))
        ctx.state.step = "MCP_START"
        ctx.jobs.submit("guides", _guide_job())
        AUDIT.action(AGENT_RES, "start_guide_search", f"destination={dest}")
        return _ok(status="submitted", destination=dest)

    async def finish_guide_search(digest_json: str = "") -> str:
        """收割攻略结果并写入共享黑板 guide_digest 分区（一步完成，digest_json 可留空）。
        digest_json 留空：系统直接采用原始收集结果（已是结构化四元+来源，无需改写）；
        也可传入整理裁剪后的 JSON 数组（保留 source_name/source_url/fetched_at/spots/foods/routes/warnings 字段）。
        写入成功后回复以「SEARCH_RESULT」开头。"""
        if ctx.reuse.get("guides") and ctx.bb.profile.guide_digest:
            ctx.state.step = "PROC_SUMMARIZE"
            return _ok(status="reused", note="攻略复用缓存，无需结构化",
                       sources=len(ctx.bb.profile.guide_digest))
        if not ctx.jobs.has("guides"):
            AUDIT.thought(AGENT_RES, "finish 自愈：start 工具未执行，自动提交搜索任务")
            ctx.jobs.submit("guides", _guide_job())
        r = await ctx.jobs.collect("guides")
        rows = r["digest"]
        if digest_json and digest_json.strip():
            try:
                parsed = json.loads(digest_json)
                if isinstance(parsed, list) and parsed:
                    rows = parsed
            except json.JSONDecodeError:
                AUDIT.observation(AGENT_RES, "digest_json 解析失败，回退原始收集结果")
        items = []
        for d in rows:
            try:
                items.append(GuideDigestItem(**d))
            except Exception:
                continue
        if not items:
            ctx.state.step = "PROC_SUMMARIZE"
            return _ok(status="error", error="没有可用的攻略条目（原始结果也为空），攻略分区将由系统护栏补齐")
        await ctx.bb.write("guide_digest", items, "researcher", "攻略搜索结构化摘要")
        ctx.state.step = "PROC_SUMMARIZE"
        spots = sorted({s for g in items for s in g.spots})[:10]
        AUDIT.observation(AGENT_RES, f"guide_digest 写入 {len(items)} 条")
        return _ok(status="written", sources=len(items), spots=spots,
                   reference_only=any(g.reference_only for g in items))

    async def search_spot_images(spots: str) -> str:
        """搜索景点配图（每景点 1-2 张，附来源），写入共享黑板 images 分区。
        spots：逗号分隔的景点名清单；留空则自动取当前草稿的全部景点。"""
        prof: TravelProfile = ctx.bb.profile
        names = [s.strip() for s in spots.split("，") if s.strip()]
        names = names or [s.strip() for s in spots.split(",") if s.strip()]
        if not names and prof.draft:
            names = sorted({s for d in prof.draft.days for s in d.spots})
        names = [n for n in names if n][:8]
        if not names:
            return _ok(status="error", error="没有可搜索的景点清单（spots 参数为空且黑板无草稿）")
        await ctx.bus.emit(AGENT_RES, f"景点配图搜索中…（{len(names)} 个景点）", "STATUS_IMAGES")
        r = await search_images(names)
        items = [ImageItem(spot=i["spot"], path=i.get("path", ""), source=i["source"], note=r.get("notice", ""))
                 for i in r["items"]]
        await ctx.bb.write("images", items, "researcher", "景点配图（" + r["mode"] + "）")
        ctx.state.step = "PLAN_PDF"
        await ctx.bus.emit(AGENT_RES,
                           f"配图完成：{len(items)} 张" + ("（本地示意配图，非实景）" if r["mode"] == "mock" else "（Wikimedia 实拍）"),
                           "STATUS_IMAGES")
        missing = [n for n in names if n not in {i.spot for i in items}]
        return _ok(status=r["mode"], images=len(items), spots=names,
                   missing=missing, notice=r.get("notice"))

    return [start_guide_search, finish_guide_search, search_spot_images]


def make_booking_tools(ctx: TeamContext):
    """MCP 专项 Agent：车票/酒店/天气三路并行查询 + 打分勾选（§4.4）。"""

    def _party(prof: TravelProfile) -> int:
        return prof.detail_info.party_size or prof.basic_info.party_size or 1

    async def _ticket_job() -> dict:
        prof: TravelProfile = ctx.bb.profile
        basic = prof.basic_info
        await ctx.bus.emit(AGENT_MCP, f"车票查询中…（{basic.origin}→{basic.destination}｜{basic.travel_mode}）", "STATUS_MCP")
        r = await query_tickets(basic.origin or "", basic.destination or "",
                                basic.travel_dates or [], basic.travel_mode or "高铁")
        tag = "已查到班次" if r["candidates"] else "无候选"
        await ctx.bus.emit(AGENT_MCP, f"车票查询完成：{tag}（{'降级参考值' if r['mode'] == 'mock' else 'MCP 实时'}）", "STATUS_MCP")
        return r

    async def _hotel_job() -> dict:
        prof: TravelProfile = ctx.bb.profile
        basic, detail = prof.basic_info, prof.detail_info
        await ctx.bus.emit(AGENT_MCP, f"酒店查询中…（{basic.destination}｜{detail.hotel.location_pref or '市中心'}）", "STATUS_MCP")
        r = await query_hotels(basic.destination or "", detail.hotel.location_pref,
                               detail.hotel.price_range, basic.budget)
        await ctx.bus.emit(AGENT_MCP, f"酒店查询完成：{len(r['candidates'])} 家候选", "STATUS_MCP")
        return r

    async def _weather_job() -> dict:
        prof: TravelProfile = ctx.bb.profile
        basic = prof.basic_info
        await ctx.bus.emit(AGENT_MCP, f"天气查询中…（{basic.destination}）", "STATUS_MCP")
        r = await query_weather(basic.destination or "", basic.travel_dates or [])
        tag = "降级参考值" if r.get("reference_only") else "实时预报"
        await ctx.bus.emit(AGENT_MCP, f"天气查询完成（{tag}）", "STATUS_MCP")
        return r

    async def start_booking_queries() -> str:
        """启动车票/酒店/天气三路并行查询（无参数：查询参数从共享黑板读取）。
        必须真正调用本工具（不允许只回复文字）；若系统提示某通道复用缓存（变更影响分析判定未受影响），该通道不重查。"""
        prof: TravelProfile = ctx.bb.profile
        basic, detail = prof.basic_info, prof.detail_info
        reuse_t = ctx.reuse.get("tickets", False) and prof.tickets
        reuse_h = ctx.reuse.get("hotels", False) and prof.hotels
        reuse_w = ctx.reuse.get("weather", False) and prof.weather
        ctx.state.step = "RES_COLLECT"
        notes = []

        if not (reuse_t or basic.travel_mode in ("自驾", "长途大巴")):
            ctx.jobs.submit("tickets", _ticket_job())
        elif reuse_t:
            notes.append("车票复用缓存（变更影响分析：未受影响，不重查）")
            AUDIT.thought(AGENT_MCP, "变更影响分析：车票复用缓存，未重查")
        if not reuse_h:
            ctx.jobs.submit("hotels", _hotel_job())
        else:
            notes.append("酒店复用缓存（变更影响分析：未受影响，不重查）")
        if not (reuse_w or not basic.travel_dates):
            ctx.jobs.submit("weather", _weather_job())
        else:
            notes.append("天气复用缓存")
        return _ok(status="submitted", notes=notes)

    async def collect_booking_results() -> str:
        """收割车票/酒店/天气查询结果：按 §4.4 公式打分、top1 自动勾选、写入黑板分区，
        返回 ORDER_RECOMMEND 摘要（候选 + 勾选理由 + 直达链接 + 数据来源）。任务未启动时自动提交（自愈）。"""
        prof: TravelProfile = ctx.bb.profile
        basic, detail = prof.basic_info, prof.detail_info
        party = _party(prof)
        reuse_notes = []
        tickets = prof.tickets  # 复用时直接取黑板缓存
        if not (ctx.reuse.get("tickets") and tickets):
            if not ctx.jobs.has("tickets"):
                AUDIT.thought(AGENT_MCP, "collect 自愈：start 工具未执行，自动提交车票查询")
                ctx.jobs.submit("tickets", _ticket_job())
            tr = await ctx.jobs.collect("tickets")
            tickets = [TicketCandidate(**t) for t in score_tickets(tr["candidates"], party)]
            await ctx.bb.write("tickets", tickets, "booking", "车票候选与勾选" + ("（降级参考值）" if tr["mode"] == "mock" else "（MCP 实时）"))
            if tr.get("notice"):
                reuse_notes.append(tr["notice"])
        else:
            reuse_notes.append("车票复用缓存（未重查）")
        hotels = prof.hotels
        if not (ctx.reuse.get("hotels") and hotels):
            if not ctx.jobs.has("hotels"):
                AUDIT.thought(AGENT_MCP, "collect 自愈：start 工具未执行，自动提交酒店查询")
                ctx.jobs.submit("hotels", _hotel_job())
            hr = await ctx.jobs.collect("hotels")
            hotels = [HotelCandidate(**h) for h in score_hotels(hr["candidates"], detail.hotel.price_range)]
            await ctx.bb.write("hotels", hotels, "booking", "酒店候选与勾选" + ("（降级参考值）" if hr["mode"] == "mock" else "（MCP 实时）"))
            if hr.get("notice"):
                reuse_notes.append(hr["notice"])
        else:
            reuse_notes.append("酒店复用缓存（未重查）")
        weather = prof.weather
        if ctx.jobs.has("weather"):
            weather = await ctx.jobs.collect("weather")
            await ctx.bb.write("weather", weather, "booking", "出行天气")
        elif not weather:
            weather = {}
        ctx.state.step = "PROC_SUMMARIZE"
        sel_t = next((t for t in tickets if t.selected), None)
        sel_h = next((h for h in hotels if h.selected), None)
        return _ok(
            notes=reuse_notes,
            selected_ticket=None if not sel_t else sel_t.model_dump(mode="json"),
            selected_hotel=None if not sel_h else sel_h.model_dump(mode="json"),
            ticket_candidates=[t.model_dump(mode="json") for t in tickets],
            hotel_candidates=[h.model_dump(mode="json") for h in hotels],
            weather_days=weather.get("days", []),
            reference_only=any(x.reference_only for x in (*tickets, *hotels)),
        )

    return [start_booking_queries, collect_booking_results]


def make_processor_tools(ctx: TeamContext):
    """信息处理 Agent：三方汇总裁剪 → 统一输入包 plan_input（§4.2）。"""

    async def write_plan_input(conflicts: list[str], notes: str) -> str:
        """把三方汇总裁剪结果写入共享黑板 plan_input 分区（统一输入包）。
        系统自动从黑板组装：用户画像 + 攻略摘要 + 车票/酒店勾选 + 天气；
        你只需提交冲突消解说明（conflicts 列表，无冲突传空）与汇总要点（notes）。
        conflicts 每条格式："字段/主题：冲突描述 → 裁决依据（以 MCP 实时数据为准 / 来源权威度）"。"""
        prof: TravelProfile = ctx.bb.profile
        resolved = {
            "basic_info": prof.basic_info.model_dump(mode="json"),
            "detail_info": prof.detail_info.model_dump(mode="json"),
            "guide_digest": [g.model_dump(mode="json") for g in prof.guide_digest],
            "tickets": [t.model_dump(mode="json") for t in prof.tickets],
            "hotels": [h.model_dump(mode="json") for h in prof.hotels],
            "weather": prof.weather,
        }
        await ctx.bb.write("plan_input", PlanInput(resolved=resolved, conflicts=conflicts),
                           "processor", notes or "三方汇总")
        ctx.state.step = "PLAN_DRAFT"
        AUDIT.observation(AGENT_PROC, f"plan_input 写入（conflicts={len(conflicts)}）")
        return _ok(status="written", conflicts=conflicts,
                   must_visit=prof.detail_info.must_visit,
                   pace=prof.detail_info.pace or "中",
                   selected_ticket=next((t.train_no for t in prof.tickets if t.selected), None),
                   selected_hotel=next((h.name for h in prof.hotels if h.selected), None))

    return [write_plan_input]


def make_planner_tools(ctx: TeamContext):
    """计划规划 Agent：草稿提交（校验+预算核算）、图片请求、PDF 生成（§4.5）。"""

    async def deliver_final() -> str:
        """生成最终行程 PDF（含逐日行程表/预算表/配图/订单清单），写入黑板 final 分区并推送完成事件。无需参数。"""
        return await _deliver_final(ctx)

    async def submit_draft(draft_json: str) -> str:
        """提交行程草稿。draft_json：JSON 数组，每天
        {"date":"YYYY-MM-DD","morning":"...","afternoon":"...","evening":"...","spots":["..."]}。
        系统校验天数/日期/必经景点/节奏并核算预算；校验失败返回错误清单，修正后重新提交。"""
        prof: TravelProfile = ctx.bb.profile
        try:
            rows = json.loads(draft_json)
            assert isinstance(rows, list) and rows
        except Exception:
            return _ok(status="error", error="draft_json 必须是非空 JSON 数组")
        try:
            draft = Draft(days=[DraftDay(**r) for r in rows])
        except Exception as e:
            return _ok(status="error", error=f"字段不符：{e}")
        errors = validate_draft(prof, draft)
        if errors:
            return _ok(status="validation_failed", errors=errors,
                       hint="请修正以上错误后重新调用 submit_draft")
        budget = compute_budget(prof, draft)
        draft.budget_items = budget["items"]
        draft.budget_total = budget["total"]
        draft.warnings = budget["warnings"]
        draft.notes = [f"预算占用 {budget['occupancy']:.0%}"] if budget["occupancy"] else []
        await ctx.bb.write("draft", draft, "planner", "行程草稿")
        await ctx.bus.emit(AGENT_PLANNER,
                           f"行程草稿已生成：{len(draft.days)} 天｜预算合计 {budget['total']} 元"
                           + (f"（占用 {budget['occupancy']:.0%}）" if budget["occupancy"] else ""),
                           "STATUS_DRAFT")
        return _ok(status="ok", total=budget["total"], occupancy=budget["occupancy"],
                   budget_max=budget["budget_max"], warnings=budget["warnings"],
                   items=budget["items"], day_count=len(draft.days))

    async def request_images() -> str:
        """按当前草稿景点清单发起图片请求（IMAGE_REQUEST，转发给信息收集 Agent）。无需参数。"""
        prof: TravelProfile = ctx.bb.profile
        if not prof.draft:
            return _ok(status="error", error="黑板无草稿，无法确定图片清单")
        spots = sorted({s for d in prof.draft.days for s in d.spots})
        ctx.state.step = "RES_IMG"
        await ctx.bus.emit(AGENT_PLANNER, f"发起图片请求：{len(spots)} 个景点", "STATUS_IMAGES")
        return _ok(status="requested", spots=spots, per_spot="1-2 张")

    return [submit_draft, request_images, deliver_final]


async def _deliver_final(ctx: TeamContext) -> str:
    """定稿：渲染 PDF + 组装订单清单 + 写 final 分区 + 完成事件（Agent 工具与护栏共用）。"""
    prof: TravelProfile = ctx.bb.profile
    if not prof.draft:
        return _ok(status="error", error="黑板无草稿")
    path = build_pdf(prof, ctx.run_id)
    orders = []
    total = 0.0
    party = prof.detail_info.party_size or prof.basic_info.party_size or 1
    for t in prof.tickets:
        if t.selected:
            amt = t.price if "往返" in t.train_no else t.price * 2 * party
            orders.append({"type": "车票", "name": t.train_no, "amount": round(amt, 1),
                           "link": t.link, "reason": t.reason, "reference_only": t.reference_only})
            total += amt
    nights = max((prof.basic_info.days or 1) - 1, 0)
    for h in prof.hotels:
        if h.selected:
            amt = h.price_per_night * nights
            orders.append({"type": "酒店", "name": h.name, "amount": round(amt, 1),
                           "link": h.link, "reason": h.reason, "reference_only": h.reference_only})
            total += amt
    final = FinalDelivery(
        pdf_path=path,
        pdf_url="/outputs/" + path.replace("\\", "/").split("/")[-1],
        order_summary=orders, total_price=round(total, 1),
        finished_at=prof.updated_at,
    )
    await ctx.bb.write("final", final, "planner", "最终交付（PDF + 订单清单）")
    ctx.state.step = "DONE"
    await ctx.bus.emit(AGENT_PLANNER, "规划完成：PDF 已生成，订单清单已就绪", "STATUS_COMPLETED",
                       pdf_url=final.pdf_url)
    return _ok(status="ok", pdf_path=path, pdf_url=final.pdf_url,
               orders=orders, total_price=final.total_price)


# ---------------------------------------------------------------------------
# TeamRunner：Team 封装为 Tool 的契约体（§3.3）+ 阶段循环 + 检查点（§3.8）
# ---------------------------------------------------------------------------

class TeamRunner:
    """聊天 Agent 经 start() 投递画像引用启动团队；团队作为后台任务运行（§3.8）。"""

    def __init__(self, bb: Blackboard, bus: StatusBus,
                 on_draft_ready: Callable[[Draft], None] | None = None,
                 on_completed: Callable[[FinalDelivery], None] | None = None,
                 on_error: Callable[[str], None] | None = None) -> None:
        self.bb = bb
        self.bus = bus
        self.on_draft_ready = on_draft_ready
        self.on_completed = on_completed
        self.on_error = on_error
        self.run_id = ""
        self._state = TeamState()
        self._jobs = JobBoard()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()          # 同一时刻仅一个团队运行
        self._awaiting_feedback = False      # 草稿待反馈（闲置等待用户）
        self._draft_rounds = 0
        self._base_version = 0               # 检查点基准版本
        self._rerun_budget = 2               # 检查点触发增量重跑的次数上限
        self.active = False                  # 规划态标志（闲置态 = False，验收 #2）

    # ---- §3.3 Tool 入参/出参契约 ----
    def start(self, task: str = "plan_trip", options: dict | None = None) -> dict:
        """Planning Team tool 入口：校验画像 → 后台启动 collect 阶段 → 立即返回受理回执。"""
        prof = self.bb.profile
        missing = prof.basic_info.missing_required()
        if missing:
            return {"status": "rejected", "reason": f"画像缺少不可默认字段：{'、'.join(missing)}"}
        if self.active and self._task and not self._task.done():
            return {"status": "rejected", "reason": "团队正在运行中，请等待当前阶段完成"}
        self.run_id = uuid.uuid4().hex
        self._draft_rounds = 0
        self._rerun_budget = 2
        self._base_version = self.bb.version()
        self.active = True
        self._awaiting_feedback = False
        self._task = asyncio.create_task(self._phase_loop("collect"))
        return {"status": "accepted", "run_id": self.run_id}

    def submit_feedback(self, feedback: str, confirmed: bool) -> dict:
        """草稿反馈路由：confirmed=True → finalize；否则 revise（≤3 轮，§4.5）。"""
        if not (self.bb.profile.draft):
            return {"status": "rejected", "reason": "当前没有待反馈的草稿"}
        if self._task and not self._task.done():
            return {"status": "rejected", "reason": "团队正在运行中，请稍候再提交反馈"}
        if confirmed:
            self._awaiting_feedback = False
            self._task = asyncio.create_task(self._phase_loop("finalize"))
            return {"status": "accepted", "phase": "finalize"}
        if self._draft_rounds >= BudgetConfig.MAX_DRAFT_ROUNDS:
            return {"status": "rejected",
                    "reason": f"草稿修改已达 {BudgetConfig.MAX_DRAFT_ROUNDS} 轮上限，请确认当前草稿或重新启动规划"}
        self._draft_rounds += 1
        self._awaiting_feedback = False
        self.bb.profile.draft_feedback = DraftFeedback(
            confirmed=False, feedback=feedback, rounds_used=self._draft_rounds)
        self._task = asyncio.create_task(self._phase_loop("revise"))
        return {"status": "accepted", "phase": "revise", "round": self._draft_rounds}

    def cancel(self, reason: str = "用户手动停止") -> dict:
        """停止当前规划任务（问题 2）：取消阶段任务与并行收集任务板，推送取消事件。

        黑板数据保留（已收集的攻略/车票/酒店不丢弃），停止后可继续补充信息并重新启动。
        幂等：无运行中任务时返回 idle 回执。
        """
        t = self._task
        had = (t is not None and not t.done()) or self.active or self._awaiting_feedback
        self.active = False
        self._awaiting_feedback = False
        self._jobs.clear()
        if t is not None and not t.done():
            t.cancel()
        asyncio.get_running_loop().create_task(self._emit_cancel(reason, had))
        return {"status": "cancelled" if had else "idle", "run_id": self.run_id}

    async def _emit_cancel(self, reason: str, had: bool) -> None:
        try:
            if had:
                await self.bus.emit("TeamRunner", f"规划任务已停止（{reason}）。已收集的数据保留，可继续补充信息后重新启动。",
                                    "STATUS_CANCELLED")
            else:
                await self.bus.emit("TeamRunner", "当前没有进行中的规划任务。", "STATUS_INFO")
        except Exception:  # noqa: BLE001 — 事件推送失败不影响停止本身
            pass

    def on_profile_changed(self) -> None:
        """用户在草稿待反馈期间修改画像：无需额外动作。

        待反馈期（_awaiting_feedback）的变更由随后的 revise 阶段直接消化（revise 读取最新黑板画像，
        Planner 修订时按新预算/新偏好重排）；团队运行中的变更由阶段边界检查点消化（§3.8）。
        若在此处再拉起 collect 重跑，会与 submit_feedback 的 revise 形成竞争，导致反馈被拒。"""
        return

    # ---- 阶段循环 ----
    async def _phase_loop(self, phase: str, changed_fields: list[str] | None = None) -> None:
        async with self._lock:
            try:
                check_budget()
                await self._run_phase(phase, changed_fields)
                if phase == "collect":
                    # §3.8 检查点：阶段边界读黑板，比对版本号，变更影响分析 → 增量重跑
                    # （revise 阶段不设检查点：草稿反馈循环本身就在消化该轮变更，避免双重重跑）
                    while self._rerun_budget > 0:
                        changes = self.bb.user_changes_since(self._base_version)
                        if not changes:
                            break
                        fields = _changed_fields(changes)
                        affected = analyze_impact(fields)
                        if affected - {"itinerary"}:
                            self._rerun_budget -= 1
                            self._base_version = self.bb.version()
                            note = (f"检查点：检测到用户变更 [{'， '.join(fields)}] → 影响分析 "
                                    f"{sorted(affected)}，增量重跑受影响环节")
                            AUDIT.thought("TeamRunner", note)
                            await self.bus.emit("TeamRunner", "检测到信息变更：" + "，".join(fields)
                                                + "｜按变更影响分析增量重跑（未受影响环节复用缓存）",
                                                "STATUS_CHECKPOINT")
                            await self._run_phase("collect", changed_fields=fields)
                        else:
                            self._base_version = self.bb.version()
                            await self.bus.emit("TeamRunner", "检测到风格/必经景点类变更 → 仅行程重排（下轮草稿体现）",
                                                "STATUS_CHECKPOINT")
                            break
                if phase in ("collect", "revise"):
                    self._base_version = self.bb.version()
                    draft = self.bb.profile.draft
                    if draft:
                        self._awaiting_feedback = True
                        if self.on_draft_ready:
                            self.on_draft_ready(draft)
                elif phase == "finalize":
                    final = self.bb.profile.final
                    self.active = False
                    if final and self.on_completed:
                        self.on_completed(final)
            except TokenBudgetExceeded as e:
                self.active = False
                await self.bus.emit("TeamRunner", f"成本控制：{e}", "STATUS_ERROR")
                if self.on_error:
                    self.on_error(str(e))
            except Exception as e:  # noqa: BLE001 — 后台任务统一兜底
                self.active = False
                AUDIT.output("TeamRunner", f"阶段运行异常：{type(e).__name__}: {e}")
                await self.bus.emit("TeamRunner", f"规划团队运行异常：{e}", "STATUS_ERROR")
                if self.on_error:
                    self.on_error(str(e))

    async def _run_phase(self, phase: str, changed_fields: list[str] | None = None) -> None:
        # 变更影响分析 → 通道复用开关（§5.3：只重跑受影响环节）
        reuse = {k: True for k in ("guides", "tickets", "hotels", "weather")}
        if changed_fields:
            affected = analyze_impact(changed_fields)
            if "guides" in affected:
                reuse["guides"] = False
            if "tickets" in affected:
                reuse["tickets"] = False
            if "hotels" in affected:
                reuse["hotels"] = False
            if "weather" in affected:
                reuse["weather"] = False
        elif phase == "collect":
            reuse = {k: False for k in reuse}  # 首次全量
        # revise / finalize：数据通道全部复用，只重排行程/出图出 PDF

        self._state = TeamState(phase=phase, step=INITIAL_STEP[phase])
        self._jobs.clear()
        ctx = TeamContext(bb=self.bb, bus=self.bus, state=self._state, jobs=self._jobs,
                          runner=self, run_id=self.run_id, reuse=reuse)
        team = self._build_team(ctx, phase)
        task_text = self._phase_task(phase, changed_fields)
        await self.bus.emit("TeamRunner",
                            {"collect": "规划团队启动（信息处理/信息收集/MCP 专项/计划规划 四 Agent 对等协同）",
                             "revise": f"草稿修订（第 {self._draft_rounds} 轮）",
                             "finalize": "草稿已确认，进入定稿流程（配图 + PDF）"}[phase],
                            "STATUS_PHASE")
        result_text, all_texts = await self._stream_team(team, task_text)
        # 确定性护栏：阶段结束以黑板为准，缺失分区直接补齐（LLM 协议执行不完美时的可靠性兜底）
        await self._ensure_sections(ctx, phase, all_texts)

    async def _ensure_sections(self, ctx: TeamContext, phase: str, texts: list[str]) -> None:
        """护栏（§7 精神的外推）：黑板分区缺失时确定性补齐，写入者仍记对应 Agent。

        草稿恢复优先级：① 对话文本中模型写好的 draft_json（工具文本化时的参数完整可提取）
        → ② _fallback_draft 确定性兜底。"""
        prof: TravelProfile = self.bb.profile
        basic, detail = prof.basic_info, prof.detail_info
        party = detail.party_size or basic.party_size or 1

        if not prof.guide_digest and basic.destination:
            r = await search_guides(basic.destination, basic.date_text or "")
            items = [GuideDigestItem(**d) for d in r["digest"]]
            await self.bb.write("guide_digest", items, "researcher", "护栏补齐：攻略分区缺失")
            await self.bus.emit("TeamRunner", "护栏：攻略分区已由确定性通道补齐", "STATUS_CHECKPOINT")
            AUDIT.observation("TeamRunner", "guardrail 补齐 guide_digest")
        if not prof.tickets and basic.origin and basic.destination:
            tr = await query_tickets(basic.origin, basic.destination,
                                     basic.travel_dates or [], basic.travel_mode or "高铁")
            tickets = [TicketCandidate(**t) for t in score_tickets(tr["candidates"], party)]
            await self.bb.write("tickets", tickets, "booking", "护栏补齐：车票分区缺失")
            await self.bus.emit("TeamRunner", "护栏：车票候选已由确定性通道补齐", "STATUS_CHECKPOINT")
        if not prof.hotels and basic.destination:
            hr = await query_hotels(basic.destination, detail.hotel.location_pref,
                                    detail.hotel.price_range, basic.budget)
            hotels = [HotelCandidate(**h) for h in score_hotels(hr["candidates"], detail.hotel.price_range)]
            await self.bb.write("hotels", hotels, "booking", "护栏补齐：酒店分区缺失")
            await self.bus.emit("TeamRunner", "护栏：酒店候选已由确定性通道补齐", "STATUS_CHECKPOINT")
        if not prof.weather and basic.travel_dates and basic.destination:
            w = await query_weather(basic.destination, basic.travel_dates)
            await self.bb.write("weather", w, "booking", "护栏补齐：天气分区缺失")
        if phase in ("collect", "revise"):
            if not prof.plan_input:
                resolved = {
                    "basic_info": basic.model_dump(mode="json"),
                    "detail_info": detail.model_dump(mode="json"),
                    "guide_digest": [g.model_dump(mode="json") for g in prof.guide_digest],
                    "tickets": [t.model_dump(mode="json") for t in prof.tickets],
                    "hotels": [h.model_dump(mode="json") for h in prof.hotels],
                    "weather": prof.weather,
                }
                await self.bb.write("plan_input", PlanInput(resolved=resolved, conflicts=["护栏组装：未经 LLM 冲突消解"]),
                                    "processor", "护栏补齐：plan_input 缺失")
            if not self.bb.profile.draft:
                draft = (self._recover_draft_from_texts(texts)
                         or _fallback_draft(self.bb.profile))
                recovered = bool(draft.notes and any("文本化" in n for n in draft.notes))
                budget = compute_budget(self.bb.profile, draft)
                draft.budget_items = budget["items"]
                draft.budget_total = budget["total"]
                draft.warnings = budget["warnings"]
                if budget["occupancy"]:
                    draft.notes = (draft.notes or []) + [f"预算占用 {budget['occupancy']:.0%}"]
                await self.bb.write("draft", draft, "planner",
                                    "护栏恢复：工具调用文本化，参数已从对话文本恢复" if recovered
                                    else "护栏补齐：草稿缺失（确定性兜底）")
                await self.bus.emit(AGENT_PLANNER,
                                    f"行程草稿已生成：{len(draft.days)} 天｜预算合计 {budget['total']} 元",
                                    "STATUS_DRAFT")
                AUDIT.observation("TeamRunner", "guardrail 恢复 draft"
                                  + ("（从文本化调用参数）" if recovered else "（确定性兜底）"))
        elif phase == "finalize":
            if not self.bb.profile.images and self.bb.profile.draft:
                spots = sorted({s for d in self.bb.profile.draft.days for s in d.spots})
                r = await search_images(spots)
                items = [ImageItem(spot=i["spot"], path=i.get("path", ""), source=i["source"], note=r.get("notice", ""))
                         for i in r["items"]]
                await self.bb.write("images", items, "researcher", "护栏补齐：图片分区缺失")
            if not self.bb.profile.final:
                if not self.bb.profile.draft:
                    draft = _fallback_draft(self.bb.profile)
                    await self.bb.write("draft", draft, "planner", "护栏补齐：草稿缺失")
                await _deliver_final(ctx)
                AUDIT.observation("TeamRunner", "guardrail 补齐 final")

    def _build_team(self, ctx: TeamContext, phase: str) -> SelectorGroupChat:
        client = get_model_client()
        proc = AssistantAgent(
            AGENT_PROC, model_client=client, tools=make_processor_tools(ctx),
            system_message=prompts.team_system_prompt(prompts.PROCESSOR_PROMPT),
            reflect_on_tool_use=True)
        res = AssistantAgent(
            AGENT_RES, model_client=client, tools=make_researcher_tools(ctx),
            system_message=prompts.team_system_prompt(prompts.RESEARCHER_PROMPT),
            reflect_on_tool_use=True)
        mcp = AssistantAgent(
            AGENT_MCP, model_client=client, tools=make_booking_tools(ctx),
            system_message=prompts.team_system_prompt(prompts.BOOKING_PROMPT),
            reflect_on_tool_use=True)
        planner = AssistantAgent(
            AGENT_PLANNER, model_client=client, tools=make_planner_tools(ctx),
            system_message=prompts.team_system_prompt(prompts.PLANNER_PROMPT),
            reflect_on_tool_use=True)
        marker = MARKER_FINAL if phase == "finalize" else MARKER_DONE
        ready_section = "final" if phase == "finalize" else "draft"
        ready_label = "最终交付" if phase == "finalize" else "行程草稿"
        baseline = self.bb.section_version(ready_section)
        termination = (TextMentionTermination(marker) |
                       SectionReadyTermination(self.bb, ready_section, ready_label, baseline) |
                       MaxMessageTermination(BudgetConfig.MAX_TEAM_TURNS))
        return SelectorGroupChat(
            [proc, res, mcp, planner],
            model_client=client,               # selector_func 恒返回名字时不消耗 LLM 调用
            selector_func=self._selector,
            termination_condition=termination,
            max_turns=BudgetConfig.MAX_TEAM_TURNS,
            allow_repeated_speaker=True,       # 连续发言上限由 _selector 控制（风险 #5）
        )

    def _recover_draft_from_texts(self, texts: list[str]) -> Draft | None:
        """从对话文本恢复模型写好的 draft_json（工具调用被文本化时的参数仍在消息里）。"""
        for text in reversed(texts):
            start = text.find('[{"date"')
            if start < 0:
                start = text.find('[{"date"'.replace("'", '"'))  # 单键名变体兜底
            if start < 0:
                continue
            # 从起点做括号配对截取完整 JSON 数组
            depth = 0
            end = -1
            in_str = False
            esc = False
            for i in range(start, len(text)):
                c = text[i]
                if esc:
                    esc = False
                    continue
                if c == "\\":
                    esc = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                continue
            try:
                rows = json.loads(text[start:end])
                if isinstance(rows, list) and rows and all(isinstance(r, dict) and "date" in r for r in rows):
                    draft = Draft(days=[DraftDay(**r) for r in rows])
                    errors = validate_draft(self.bb.profile, draft)
                    if errors:
                        AUDIT.observation("TeamRunner", f"文本化草稿校验未过（{errors}），转兜底")
                        return None
                    draft.notes = (draft.notes or []) + ["由护栏从对话文本恢复（工具调用文本化）"]
                    return draft
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    def _selector(self, messages: list) -> str:
        """确定性主持人（§3.4：只决定下一个该谁发言）。

        正常路径由工具推进 TeamState.step；对纯文本发言的阶段（广播/启动）与空转（该收割却只发文字）
        做幂等兜底推进，防止模型跳过工具导致状态机卡死。连续发言 >3 次且非收敛目标时强制收敛（风险 #5）。
        """
        state = self._state
        if messages:
            last_src = getattr(messages[-1], "source", "")
            last_txt = getattr(messages[-1], "to_text", lambda: "")() or ""
            if state.step == "PROC_BROADCAST" and last_src == AGENT_PROC:
                state.step = "RES_START"      # 广播完成 → 信息收集先启动
            elif state.step == "RES_START" and last_src == AGENT_RES:
                state.step = "MCP_START"      # 搜索启动 → MCP 专项并行启动
            elif state.step == "MCP_START" and last_src == AGENT_MCP:
                state.step = "RES_COLLECT"    # 查询启动 → 双方收割
            elif (state.step == "RES_COLLECT" and last_src == AGENT_RES
                  and "SEARCH_RESULT" not in last_txt and state.consecutive >= 2):
                state.step = "MCP_COLLECT"    # 收割空转两轮 → 交给下一环节（攻略走护栏补齐）
                AUDIT.thought("Selector", "Researcher 收割空转，推进到 MCP 收割（攻略由护栏兜底）")
            elif (state.step == "MCP_COLLECT" and last_src == AGENT_MCP
                  and "ORDER_RECOMMEND" not in last_txt and state.consecutive >= 2):
                state.step = "PROC_SUMMARIZE"
                AUDIT.thought("Selector", "BookingButler 收割空转，推进到信息汇总（订单由护栏兜底）")
            elif (state.step == "PROC_SUMMARIZE" and last_src == AGENT_PROC
                  and "PLAN_REQUEST" not in last_txt and state.consecutive >= 2):
                state.step = "PLAN_DRAFT"
                AUDIT.thought("Selector", "Processor 汇总空转，推进到行程规划（plan_input 由护栏兜底）")
            elif (state.step == "PLAN_IMGREQ" and last_src == AGENT_PLANNER
                  and "IMAGE_REQUEST" not in last_txt and state.consecutive >= 2):
                state.step = "RES_IMG"
            elif (state.step == "RES_IMG" and last_src == AGENT_RES
                  and "IMAGE_RESULT" not in last_txt and state.consecutive >= 2):
                state.step = "PLAN_PDF"
        speaker = SPEAKER.get(state.step, AGENT_PROC)
        if speaker == state.last_speaker:
            state.consecutive += 1
        else:
            state.consecutive = 1
            state.last_speaker = speaker
        if state.consecutive > 3 and speaker != AGENT_PLANNER:
            AUDIT.thought("Selector", f"{speaker} 连续发言 {state.consecutive} 次，强制收敛到 {AGENT_PLANNER}")
            speaker, state.last_speaker, state.consecutive = AGENT_PLANNER, AGENT_PLANNER, 1
        return speaker

    def _phase_task(self, phase: str, changed_fields: list[str] | None) -> str:
        prof = self.bb.profile
        if phase == "collect":
            head = "TASK_BROADCAST（团队启动，run_id=%s）%s" % (
                self.run_id,
                f"｜增量重跑：用户变更 [{'， '.join(changed_fields)}]，未受影响环节复用缓存" if changed_fields else "")
            return head + "\n当前共享黑板画像：\n" + self.bb.compact_json() + \
                "\n请信息处理 Agent 按协议开始：先广播任务，再由信息收集与 MCP 专项并行收集，汇总后交计划规划出草稿。"
        if phase == "revise":
            fb = prof.draft_feedback.feedback if prof.draft_feedback else ""
            from .planning import draft_summary_text
            return ("DRAFT_FEEDBACK 用户对草稿的反馈：" + fb +
                    "\n当前草稿概要：\n" + draft_summary_text(prof.draft) +
                    f"\n（这是第 {self._draft_rounds} 轮修改，共上限 {BudgetConfig.MAX_DRAFT_ROUNDS} 轮）"
                    "\n请计划规划 Agent 按反馈修订行程并重新调用 submit_draft。")
        return "DRAFT_CONFIRMED 用户已确认草稿。请计划规划 Agent 调用 request_images 发起图片请求，" \
               "待 IMAGE_RESULT 后调用 generate_pdf 完成定稿。"

    async def _stream_team(self, team: SelectorGroupChat, task_text: str) -> tuple[str, list[str]]:
        """事件流采集：Thought/Action/Observation → 审计日志（验收 #16）；Agent 最终消息 → 用户时间线。

        返回（最后一条消息文本, 全部消息文本列表）——后者供护栏从文本化调用中恢复参数。
        """
        from autogen_agentchat.messages import ThoughtEvent
        last_text = ""
        texts: list[str] = []
        stream = team.run_stream(task=task_text)
        while True:
            try:
                msg = await stream.__anext__()
            except StopAsyncIteration:
                break
            if isinstance(msg, ThoughtEvent):
                AUDIT.thought(msg.source, msg.content)
            elif isinstance(msg, ToolCallRequestEvent):
                for call in msg.content:
                    if getattr(call, "name", None):
                        AUDIT.action(msg.source, call.name, str(getattr(call, "arguments", "")))
            elif isinstance(msg, ToolCallExecutionEvent):
                obs = "; ".join(getattr(c, "content", "") or "" for c in msg.content)[:400]
                AUDIT.observation(msg.source, obs)
            elif isinstance(msg, BaseAgentEvent):
                pass
            elif isinstance(msg, BaseChatMessage):
                last_text = msg.to_text()
                if msg.source != "user":
                    from .chatter import clean_reply
                    last_text = clean_reply(last_text, fallback="（已处理）")
                    texts.append(last_text)
                    await self.bus.emit(msg.source, _clip_msg(last_text), "AGENT_MESSAGE")
                    AUDIT.output(msg.source, last_text)
        return last_text, texts


def _changed_fields(changes) -> list[str]:
    """changelog 条目 → 变更字段名清单（detail_info 的字段带分区前缀，与 FIELD_IMPACT 键对齐）。"""
    fields = []
    for e in changes:
        f = f"{e.section}.{e.field}" if e.section == "detail_info" else e.field
        if f not in fields:
            fields.append(f)
    return fields


def _fallback_draft(prof: TravelProfile) -> Draft:
    """确定性兜底草稿：攻略经典路线 + 必经景点硬约束 + 节奏约束（护栏用，不依赖 LLM）。"""
    from .mocks.data import expand_dates
    kb = kb_for_city(prof.basic_info.destination or "")
    days = prof.basic_info.days or 3
    lo, _hi = PACE_SPOTS.get(prof.detail_info.pace or "中", (3, 3))
    must = list(prof.detail_info.must_visit)
    pool: list[str] = []
    pool += must
    for route in kb["routes"]:
        for token in route.replace("→", " ").replace("(", " ").replace(")", " ").split():
            if 2 <= len(token) <= 8 and token not in pool and not token.startswith("D"):
                pool.append(token)
    for s in kb["spots"]:
        if s not in pool:
            pool.append(s)
    # 去掉明显的非景点 token（时段词/交通词/动作词）
    junk = ("乘", "抵", "入住", "游览", "酒店", "夜宵", "返程", "往返", "茶社",
            "上午", "下午", "晚上", "中午", "清晨", "继续", "周边", "上午)", "下午)",
            "自由活动", "漫步", "上午→", "火锅", "小吃", "夜景", "午餐", "晚餐")
    pool = [s for s in pool if s and not any(j in s for j in junk)][: days * lo * 2]
    dates = prof.basic_info.travel_dates or expand_dates("2026-10-01", days)
    dd = []
    idx = 0
    for i in range(days):
        spots = pool[idx: idx + lo]
        idx += lo
        if i == days - 1:
            spots = spots[: max(lo - 1, 1)]  # 末日留返程时段
        joined = " → ".join(spots) if spots else "自由活动 / 市区漫步"
        dd.append(DraftDay(
            date=dates[i] if i < len(dates) else f"第{i + 1}天",
            morning=f"游览 {spots[0]}" if spots else "自由活动",
            afternoon=f"继续 {spots[1]}" if len(spots) > 1 else "周边漫步",
            evening="品尝当地美食" if i < days - 1 else "收拾行李返程",
            spots=spots,
        ))
    return Draft(days=dd)


def _clip_msg(text: str, limit: int = 160) -> str:
    t = text.replace("\n", " ")
    return t if len(t) <= limit else t[: limit - 1] + "…"
