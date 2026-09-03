"""Designer 版面设计师：确定性外循环 + 第 6 Agent 编排（docs/html-designer-plan.md D4/D7）。

架构要点（审核修订，勿退化）：
- Designer 不进 SelectorGroupChat：由 _deliver_final 分流后调用本模块的 designer_chain；
  状态机/终止条件零改动（该 selector 有实测死锁史）
- 与 AutoGen 解耦：agent_factory/budget_check 由调用方注入，无 autogen 环境可全量单测
- 缓存 = 内容寻址指纹（进提示词的分区投影 + 管线版本号 sha256），不用 run_id+黑板版本号
- 工具：write_html（哨兵/限额校验→消毒→套壳落盘，只回元数据不回显全文）、
        render_pdf（无参，渲染+诊断，返回有界 JSON）
- 本模块顶层不得 import autogen（AutoGen 依赖在 make_autogen_factory 内延迟导入）
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import OUTPUT_DIR
from .design import DESIGNER_PIPELINE_VERSION, DESIGNER_TEMPLATE_META, DEFAULT_THEME, THEMES
from .models import TravelProfile
from .status import AUDIT
from .tools.htmlpdf import (HtmlTooLargeError, render_and_inspect, sanitize_html, wrap_html)

AGENT_DESIGNER = "Designer"

DESIGNER_TIMEOUT_S = 600.0     # 整链熔断（最坏一次主备调用 450s + 一次正常调用）
SOFT_CHECKPOINT_S = 240.0      # 软检查点：超时仍无 HTML 落盘 → 跳过剩余修正轮直接回退
MAX_ATTEMPTS = 3               # 1 生成 + ≤2 修正（消毒/截断/渲染/诊断共享预算，非各自 2 轮）

HTML_DIR = OUTPUT_DIR / "html"
CACHE_DIR = OUTPUT_DIR / "htmlcache"
SENTINEL = "<!--TRIPMATE-END-->"
KEYWORDS = ("行程", "预算", "订单")

# 依赖注入类型：agent_factory(system_prompt, tools) -> 具备 await run(task) 的对象
AgentFactory = Callable[[str, list], Any]
BudgetCheck = Callable[[], None]


class DesignerError(RuntimeError):
    """Designer 链最终失败（调用方回退模板链；CancelledError 不在此列，原样穿透）。"""


@dataclass
class DesignerResult:
    pdf_path: str
    html_path: str
    engine: str
    attempts: int
    from_cache: bool = False
    findings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 快照与缓存键（D4：内容寻址）
# ---------------------------------------------------------------------------

def _image_uri(path: str) -> str:
    """黑板图片路径（Windows 绝对路径）→ file:/// URI；不存在/非法返回空串（Agent 据此降级占位）。"""
    if not path:
        return ""
    try:
        p = Path(path)
        return p.resolve().as_uri() if p.exists() else ""
    except (OSError, ValueError):
        return ""


def designer_snapshot(prof: TravelProfile) -> dict:
    """进 Designer 提示词的分区投影（不用 compact_json：避免 plan_input 数据副本与 changelog）。

    图片路径在此统一转 file:/// URI（as_uri 正确处理中文/空格的百分号编码），Agent 照抄，
    禁止自行拼路径。
    """
    draft = prof.draft
    basic = prof.basic_info
    detail = prof.detail_info
    sel_tickets = [t.model_dump(mode="json") for t in prof.tickets if t.selected]
    sel_hotels = [h.model_dump(mode="json") for h in prof.hotels if h.selected]
    for h in sel_hotels:
        h["image_uri"] = _image_uri(h.get("image_path", ""))
    foods: list[str] = []
    guide_sources: list[str] = []
    guide_warnings: list[str] = []
    for g in prof.guide_digest:
        guide_sources.append(g.source_name)
        guide_warnings.extend(g.warnings)
        for f in g.foods:
            if f not in foods:
                foods.append(f)
    return {
        "basic": basic.model_dump(mode="json", exclude={"template"}),
        "detail": detail.model_dump(mode="json"),
        "draft": draft.model_dump(mode="json") if draft else None,
        "selected_tickets": sel_tickets,
        "selected_hotels": sel_hotels,
        "ticket_candidate_count": len(prof.tickets),
        "hotel_candidate_count": len(prof.hotels),
        "weather": prof.weather,
        "images": [{"spot": i.spot, "uri": _image_uri(i.path), "source": i.source}
                   for i in prof.images],
        "foods": foods[:12],
        "guide_sources": guide_sources[:5],
        "guide_warnings": guide_warnings[:6],
        "reference_only": any(t.reference_only for t in prof.tickets if t.selected)
        or any(h.reference_only for h in prof.hotels if h.selected),
    }


def designer_cache_key(prof: TravelProfile) -> str:
    """内容寻址缓存键：快照投影 + 管线版本号的 sha256（同一份数据重复定稿命中，数据变更 miss）。

    注意时机：必须在写 final 分区之前取（final 写入会 +1 黑板版本，但不进快照所以无影响——
    这正是弃用黑板版本号作键的原因）。
    """
    payload = {"v": DESIGNER_PIPELINE_VERSION, "mode": "designer",
               "snap": designer_snapshot(prof)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Designer 工具组（AutoGen 无关的闭包；state 由外循环持有）
# ---------------------------------------------------------------------------

@dataclass
class DesignerIO:
    """工具闭包共享的落盘状态（外循环读写，跨修正轮保持）。"""

    run_id: str
    dest: str
    html_path: Path = None  # type: ignore[assignment]
    pdf_path: Path = None  # type: ignore[assignment]
    last_render: dict = field(default_factory=dict)
    last_findings: list[str] = field(default_factory=list)
    last_tool_error: str = ""   # write_html 最近一次失败原因（截断/超限），修正轮任务携带
    days: int | None = None     # 行程天数，诊断页数警告带用（D3：2..days*6+6）
    wrote_html: bool = False

    def __post_init__(self) -> None:
        self.html_path = self.html_path or (HTML_DIR / f"trip_{self.run_id[:8]}.html")
        self.pdf_path = self.pdf_path or _pdf_path_for(self.dest, self.run_id)


def _pdf_path_for(dest: str, run_id: str) -> Path:
    import re
    safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", (dest or "行程").strip()) or "行程"
    return OUTPUT_DIR / f"行程计划_{safe}_{run_id[:8]}_ai.pdf"


def parse_theme_line(html: str) -> tuple[str, str]:
    """剥离首行 THEME: 声明（v2 输出契约），返回 (theme, 余下内容)。

    非法主题名同样剥掉声明行（评审 #8：泄漏为正文可见文本），回退默认主题。
    """
    text = html.lstrip("\ufeff \t\r\n")
    if text[:6].upper() == "THEME:":
        line, _, rest = text.partition("\n")
        theme = line.split(":", 1)[1].strip()
        return (theme if theme in THEMES else DEFAULT_THEME), rest.lstrip("\r\n")
    return DEFAULT_THEME, text


def make_designer_tools(io: DesignerIO) -> list:
    """write_html / render_pdf 两个工具（契约见 D2/D4：write_html 不回显全文，render_pdf 无参）。"""

    async def write_html(html: str) -> str:
        """提交版面 HTML（body 片段）。片段须以 <!--TRIPMATE-END--> 结尾；首行可用
        "THEME: theme-azure|theme-warm|theme-fresh|theme-mono" 声明主题。
        系统负责消毒与套壳，成功后返回元数据；随后必须调用 render_pdf 查看诊断。"""
        html = html or ""
        truncated = SENTINEL not in html
        if truncated:
            # 哨兵缺失即判截断（比消毒失败更精确），不烧渲染
            io.last_tool_error = "write_html 截断失败：未找到 TRIPMATE-END 哨兵"
            return json.dumps({"status": "truncated", "error":
                               "输出在结束前被截断（未找到 TRIPMATE-END 哨兵）。请更精简地重新输出完整片段："
                               "压缩文案、限制 SVG 复杂度，必要时省略非核心章节。"}, ensure_ascii=False)
        body = html.split(SENTINEL, 1)[0]
        theme, body = parse_theme_line(body)
        try:
            result = sanitize_html(body)
        except HtmlTooLargeError as exc:
            io.last_tool_error = f"write_html 超限：{exc}"
            return json.dumps({"status": "too_large", "error": str(exc)}, ensure_ascii=False)
        wrapped = wrap_html(result.html, title=f"行程计划_{io.dest or ''}", theme=theme)
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        io.html_path.write_text(wrapped, encoding="utf-8")
        io.wrote_html = True
        io.last_tool_error = ""
        io.last_findings = result.findings
        return json.dumps({
            "status": "written", "html_path": str(io.html_path),
            "bytes": len(wrapped.encode("utf-8")), "theme": theme,
            "sanitize_findings": result.findings[:8],
            "removed_nodes": result.removed_nodes,
            "note": "消毒拦截属正常安全行为（外链/脚本剥除）；下一步调用 render_pdf 获取诊断。",
        }, ensure_ascii=False)

    async def render_pdf() -> str:
        """渲染当前已提交的 HTML 为 PDF 并做确定性诊断。无需参数；不达标时按返回的
        修正建议修改后再次 write_html + render_pdf（最多 2 轮修正）。"""
        if not io.wrote_html or not io.html_path.exists():
            return json.dumps({"ok": False, "error": "尚无已提交的 HTML，请先调用 write_html"},
                              ensure_ascii=False)
        report = await asyncio.to_thread(
            render_and_inspect, io.html_path, io.pdf_path, KEYWORDS, io.days)
        io.last_render = report
        if report.get("ok"):
            return json.dumps({"ok": True, "pdf_path": report["pdf_path"],
                               "engine": report.get("engine"), "pages": report.get("pages"),
                               "warnings": report.get("warnings", [])}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": report.get("error"),
                           "missing_keywords": report.get("missing_keywords"),
                           "advice": "确保包含「行程总览/逐日行程」「预算」「推荐订单」章节标题文字；"
                                     "内容精简、卡片不超过 40 个图片引用。"},
                          ensure_ascii=False)

    return [write_html, render_pdf]


# ---------------------------------------------------------------------------
# 确定性外循环（D7）
# ---------------------------------------------------------------------------

def _task_text_gen(snap: dict) -> str:
    """生成轮任务文本：片段库 + 金样（仅此轮携带，修正轮不带，控制隐性 token 成本）+ 快照。"""
    import json as _json
    from .design import load_golden_sample
    from .design.fragments import COMPONENT_FRAGMENTS
    from .prompts import DESIGNER_PROMPT
    return (DESIGNER_PROMPT + "\n\n【组件片段库】\n" + COMPONENT_FRAGMENTS +
            "\n\n【金样（质量标杆，仿结构与表达密度，内容必须来自快照）】\n" +
            load_golden_sample() +
            "\n\n【黑板快照（唯一数据来源，禁止编造；图片 src 只用其中的 uri）】\n" +
            _json.dumps(snap, ensure_ascii=False, default=str) +
            "\n\n请开始排版：先调用 write_html 提交完整 body 片段（以 TRIPMATE-END 结尾），"
            "再调用 render_pdf 查看诊断；诊断不达标则修正后重交（最多 2 轮修正）。")


def _task_text_fix(state: dict, snap: dict | None = None) -> str:
    """修正轮任务文本：诊断摘要 + 上轮工具错误 + 最小快照（不回传 HTML 全文）。

    每轮是全新无记忆的 Agent 实例（上下文膨胀控制），不带快照则拿不到图片 uri
    与组件数据、修正不可执行——快照是修正轮的必要输入（片段库/金样仍不带）。
    """
    prev = state.get("render") or {}
    findings = state.get("findings") or []
    tool_error = state.get("tool_error") or ""
    text = ("上一版 HTML 未通过渲染诊断，请修正后重新提交完整 body 片段（write_html + render_pdf）。\n"
            f"上一版文件：{state.get('html_path', '')}\n"
            f"诊断：{json.dumps(prev, ensure_ascii=False)[:400]}\n"
            f"消毒发现：{json.dumps(findings[:8], ensure_ascii=False)[:300]}\n")
    if tool_error:
        text += f"工具错误：{tool_error[:200]}\n"
    if snap is not None:
        text += ("【黑板快照（唯一数据来源，禁止编造；图片 src 只用其中的 uri）】\n"
                 f"{json.dumps(snap, ensure_ascii=False, default=str)}\n")
    text += ("注意：缺少章节关键词时补齐对应章节标题；被消毒剥除的元素请改用合规写法；"
             "输出仍以 TRIPMATE-END 结尾。")
    return text


async def designer_chain(prof: TravelProfile, run_id: str, agent_factory: AgentFactory,
                         system_prompt: str,
                         budget_check: BudgetCheck | None = None,
                         bus: Any = None,
                         cache_key: str | None = None) -> DesignerResult:
    """生成→消毒套壳→渲染→诊断→修正（≤2 轮）的确定性外循环。

    - budget_check 默认 lazy-import llm.check_budget（共享单例客户端，逐轮检查）
    - 任何一轮失败路径（截断/消毒异常/渲染失败/关键词缺失）共享 MAX_ATTEMPTS 预算
    - CancelledError 不捕获（用户停止语义，由调用方放行）
    - 最终失败抛 DesignerError → 调用方回退模板链
    """
    dest = prof.basic_info.destination or ""
    io = DesignerIO(run_id=run_id, dest=dest)
    snap = designer_snapshot(prof)
    io.days = len(prof.draft.days) if prof.draft and prof.draft.days else None

    async def _emit(text: str) -> None:
        if bus is not None:
            try:
                await bus.emit(AGENT_DESIGNER, text, "STATUS_PROGRESS")
            except Exception:  # noqa: BLE001 — 状态推送失败不影响主流程
                pass

    # 缓存：同数据重复定稿直接复用 HTML 重渲染（不重新生成，D4）
    key = cache_key or designer_cache_key(prof)
    cached = CACHE_DIR / f"{key}.html"
    if cached.exists():
        await _emit("命中同版式缓存，直接重渲染…")
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        io.html_path.write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
        io.wrote_html = True
        report = await asyncio.to_thread(
            render_and_inspect, io.html_path, io.pdf_path, KEYWORDS, io.days)
        if report.get("ok"):
            return DesignerResult(pdf_path=report["pdf_path"], html_path=str(io.html_path),
                                  engine=report.get("engine", ""), attempts=0, from_cache=True)
        AUDIT.output(AGENT_DESIGNER, f"缓存命中但渲染失败，转正常生成：{report.get('error')}")

    if budget_check is None:
        from .llm import check_budget as budget_check  # noqa: N813 — 共享单例，逐轮检查
    try:
        # 顶层不得 import llm（含 autogen 依赖）：延迟到链路启动时
        from .llm import TokenBudgetExceeded as _BudgetExceeded
    except ImportError:  # 无 autogen 环境：预算熔断只可能来自显式注入的 budget_check
        _BudgetExceeded = None  # type: ignore[assignment]

    t0 = time.monotonic()
    attempt = 0
    state: dict = {"render": None, "findings": [], "tool_error": "",
                   "html_path": str(io.html_path)}
    last_error = "Designer 未产出任何渲染结果"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        budget_check()
        if attempt > 1 and not io.wrote_html and (time.monotonic() - t0) > SOFT_CHECKPOINT_S:
            AUDIT.output(AGENT_DESIGNER, f"软检查点：{SOFT_CHECKPOINT_S:.0f}s 仍无 HTML 落盘，停止修正轮")
            break
        await _emit(f"🎨 第 {attempt}/{MAX_ATTEMPTS} 轮版面设计中…")
        tools = make_designer_tools(io)
        agent = agent_factory(system_prompt, tools)
        task = _task_text_gen(snap) if attempt == 1 else _task_text_fix(state, snap)
        try:
            await agent.run(task=task)
        except Exception as exc:  # noqa: BLE001 — 模型调用失败计入共享预算，下一轮或回退
            if _BudgetExceeded is not None and isinstance(exc, _BudgetExceeded):
                # 预算耗尽：后续每轮首行的 budget_check() 必再抛，重试纯烧延迟，
                # 直接结束循环走回退（D4：超预算仍产出模板 PDF，而非终止交付）
                raise DesignerError(f"token 预算耗尽，停止修正直接回退：{exc}")
            last_error = f"Agent 运行失败（{type(exc).__name__}: {exc}）"
            AUDIT.output(AGENT_DESIGNER, last_error)
            continue
        # 同步工具闭包产出的最新状态 → 修正轮任务文本依赖真实诊断（评审 #1）
        state["render"] = io.last_render
        state["findings"] = io.last_findings
        state["tool_error"] = io.last_tool_error
        if io.last_render.get("ok"):
            last_error = ""
            break
        last_error = json.dumps(io.last_render, ensure_ascii=False)[:400] or "渲染诊断未通过"
        AUDIT.observation(AGENT_DESIGNER, f"第 {attempt} 轮未达标：{last_error}")

    if not (io.wrote_html and io.last_render.get("ok")):
        raise DesignerError(last_error)

    HTML_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cached.write_text(io.html_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:  # noqa: BLE001 — 缓存写失败不影响交付
        AUDIT.output(AGENT_DESIGNER, "HTML 缓存写入失败（不影响本次交付）")
    return DesignerResult(pdf_path=io.last_render["pdf_path"], html_path=str(io.html_path),
                          engine=io.last_render.get("engine", ""), attempts=attempt,
                          findings=io.last_findings)


# ---------------------------------------------------------------------------
# AutoGen 工厂（延迟导入：无 autogen 环境不影响本模块其余部分）
# ---------------------------------------------------------------------------

def make_autogen_factory():
    """生产环境的 agent_factory 构造器：共享单例模型客户端（记账/主备切换免费继承）。

    客户端的获取延迟到真正生成轮（factory 被调用时）——保证 make_autogen_factory()
    本身无副作用，可在未配置 LLM 的环境下先行创建（回退链测试依赖此语义）。
    """

    def factory(system_prompt: str, tools: list):
        from autogen_agentchat.agents import AssistantAgent

        from .llm import get_model_client

        # reflect_on_tool_use=False：工具结果以摘要回到上下文，省一次反思调用；
        # max_tool_iterations=3-4：写→渲→修一轮内完成（审核 #12）
        return AssistantAgent(
            AGENT_DESIGNER, model_client=get_model_client(), tools=tools,
            system_message=system_prompt, reflect_on_tool_use=False,
            max_tool_iterations=4)

    return factory


__all__ = ["AGENT_DESIGNER", "DESIGNER_TEMPLATE_META", "DesignerError", "DesignerResult",
           "DesignerIO", "designer_chain", "designer_cache_key", "designer_snapshot",
           "make_designer_tools", "make_autogen_factory", "parse_theme_line",
           "DESIGNER_TIMEOUT_S", "SENTINEL", "KEYWORDS"]
