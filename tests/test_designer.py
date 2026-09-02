"""Designer 链单测（docs/html-designer-plan.md 任务 2.3/2.4，无 LLM、无 autogen）：

快照/缓存键内容寻址、write_html/render_pdf 工具契约、确定性外循环
（成功/截断重试/最终失败回退信号/软行为）、整链与 AutoGen 解耦（假 agent_factory）。
"""

import json
from pathlib import Path

import pytest

from tripmate import designer as dmod
from tripmate.designer import (DesignerError, DesignerIO, designer_cache_key,
                               designer_chain, designer_snapshot, make_designer_tools)
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, HotelCandidate,
                             ImageItem, TicketCandidate, TravelProfile)
from tripmate.tools.htmlpdf import _discover_chromium, _find_edge

_RENDER_AVAILABLE = True
try:
    _RENDER_AVAILABLE = bool(_discover_chromium() or _find_edge())
except Exception:  # noqa: BLE001
    _RENDER_AVAILABLE = False

RENDER_SKIP = pytest.mark.skipif(not _RENDER_AVAILABLE, reason="本机无 Chromium/Edge 内核")


@pytest.fixture(autouse=True)
def _isolated_designer_dirs(tmp_path, monkeypatch):
    """每个测试独立的 HTML/缓存目录：内容寻址缓存会让同 profile 的用例互相命中。"""
    monkeypatch.setattr(dmod, "HTML_DIR", tmp_path / "html")
    monkeypatch.setattr(dmod, "CACHE_DIR", tmp_path / "htmlcache")


# ---------- 测试画像（对应 tests/test_pdf.py 的成都 fixture 精简版）----------

def _profile() -> TravelProfile:
    prof = TravelProfile()
    prof.basic_info = BasicInfo(origin="上海", destination="成都", days=2, travel_mode="高铁",
                                travel_dates=["2026-10-01", "2026-10-02"], style=["休闲"],
                                budget=5000, party_size=2)
    prof.detail_info = DetailInfo(must_visit=["大熊猫基地"], pace="中", party_size=2)
    prof.tickets = [TicketCandidate(train_no="D636", depart_time="09:15", arrive_time="22:40",
                                    duration_min=805, price=609.0, link="https://kyfw.12306.cn",
                                    selected=True, reference_only=True)]
    prof.hotels = [HotelCandidate(name="亚朵酒店", price_per_night=488.0, distance_km=0.6,
                                  rating=4.8, link="https://hotels.ctrip.com", selected=True,
                                  reference_only=True, image_path="")]
    prof.draft = Draft(days=[
        DraftDay(date="2026-10-01", morning="乘 D636 赴蓉", afternoon="入住春熙路",
                 evening="太古里夜景", spots=["春熙路太古里"]),
        DraftDay(date="2026-10-02", morning="大熊猫基地", afternoon="宽窄巷子",
                 evening="返程", spots=["大熊猫基地", "宽窄巷子"]),
    ])
    return prof


class _FakeAgent:
    """按脚本依次执行工具调用的假 Agent（替代 AutoGen AssistantAgent）。"""

    def __init__(self, tools: list, script: list):
        self._tools = {t.__name__: t for t in tools}
        self._script = script

    async def run(self, task: str = "") -> None:
        for action in self._script:
            name, arg = (action if isinstance(action, tuple) else (action, None))
            # render_pdf 无参契约（审核 #12）；write_html 接收片段
            result = await (self._tools[name](arg) if arg is not None else self._tools[name]())


def _factory(script_for_attempt: dict[int, list]):
    calls = {"n": 0}

    def factory(system_prompt: str, tools: list):
        calls["n"] += 1
        return _FakeAgent(tools, script_for_attempt.get(calls["n"], script_for_attempt.get("default", [])))

    factory.calls = calls
    return factory


def _valid_fragment(prof: TravelProfile) -> str:
    """带必备关键词的最小合规片段（图片用白名单外的假 URI 验证消毒不阻塞交付）。"""
    img = prof.images[0] if prof.images else None
    src = f'src="{img.path}" ' if img else ""
    return (f'THEME: theme-warm\n<section class="cover"><h1 class="cover-title">成都行程</h1></section>'
            f'<section class="sec"><h2>行程总览</h2><p>两日行程安排紧凑。</p></section>'
            f'<img {src}alt="示意">'
            f'<section class="sec"><h2>预算一览</h2><p>预算合计 ¥1000。</p></section>'
            f'<section class="sec"><h2>推荐订单</h2><p>车票与酒店订单如下。</p></section>'
            f'<!--TRIPMATE-END-->')


# ---------- 快照与缓存键 ----------

def test_snapshot_projects_partitions_and_converts_image_uri(tmp_path):
    prof = _profile()
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    prof.images = [ImageItem(spot="大熊猫基地", path=str(img), source="实拍")]
    snap = designer_snapshot(prof)
    assert snap["basic"]["destination"] == "成都"
    assert "template" not in snap["basic"]
    assert snap["selected_tickets"][0]["train_no"] == "D636"
    assert snap["images"][0]["uri"].startswith("file:///")
    assert "changelog" not in json.dumps(snap)  # 不携带全局 dump 的包袱
    assert snap["reference_only"] is True


def test_snapshot_missing_image_uri_empty():
    prof = _profile()
    prof.images = [ImageItem(spot="大熊猫基地", path="", source="本地示意配图")]
    snap = designer_snapshot(prof)
    assert snap["images"][0]["uri"] == ""


def test_cache_key_content_addressed(tmp_path):
    prof = _profile()
    k1 = designer_cache_key(prof)
    k2 = designer_cache_key(prof)
    assert k1 == k2  # 同数据同键
    prof.draft.days[0].morning = "改了行程"
    assert designer_cache_key(prof) != k1  # 数据变更换键
    # 写 final 分区（+1 黑板版本）不影响键 —— 弃用版本号作键的核心原因
    prof2 = _profile()
    before = designer_cache_key(prof2)
    prof2.final = None
    prof2.version += 99
    prof2.updated_at = "changed"
    assert designer_cache_key(prof2) == before
    # 模板切换不影响 designer 键（designer 输出不依赖 template 值本身）
    prof3 = _profile()
    prof3.basic_info.template = "designer"
    prof3.basic_info.template = None
    assert designer_cache_key(prof3) == before


# ---------- 工具契约 ----------

@RENDER_SKIP
def test_write_html_rejects_truncated_and_returns_metadata_only(tmp_path):
    prof = _profile()
    io = DesignerIO(run_id="testrun01", dest="成都")
    tools = {t.__name__: t for t in make_designer_tools(io)}

    async def _call():
        bad = await tools["write_html"]('<section>没有哨兵的截断输出')
        good = json.loads(await tools["write_html"](_valid_fragment(prof)))
        return json.loads(bad), good

    bad, good = _run(_call())
    assert bad["status"] == "truncated"
    assert good["status"] == "written"
    assert good["theme"] == "theme-warm"
    assert io.html_path.exists()
    wrapped = io.html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in wrapped and "Microsoft YaHei" in wrapped
    assert "TRIPMATE-END" not in wrapped  # 哨兵与 THEME 行被剥除，正文保留


def test_render_pdf_without_html_errors():
    io = DesignerIO(run_id="testrun02", dest="成都")
    tools = {t.__name__: t for t in make_designer_tools(io)}

    import asyncio
    out = json.loads(asyncio.run(tools["render_pdf"]()))
    assert out["ok"] is False and "write_html" in out["error"]


# ---------- 确定性外循环（D7）----------

@RENDER_SKIP
def test_chain_success_first_attempt(tmp_path):
    prof = _profile()
    script = _factory({1: [("write_html", _valid_fragment(prof)), "render_pdf"]})
    result = _run(designer_chain(prof, "run_ok_a", script, system_prompt="sys",
                                 budget_check=lambda: None))
    assert result.pdf_path and Path(result.pdf_path).exists()
    assert result.attempts == 1 and not result.from_cache
    assert Path(result.pdf_path).stat().st_size > 1000


@RENDER_SKIP
def test_chain_retries_after_truncation_then_succeeds(tmp_path):
    prof = _profile()
    script = _factory({1: [("write_html", "<section>截断了没有哨兵")],  # 第 1 轮截断
                       2: [("write_html", _valid_fragment(prof)), "render_pdf"]})
    result = _run(designer_chain(prof, "run_ok_b", script, system_prompt="sys",
                                 budget_check=lambda: None))
    assert result.attempts == 2 and result.pdf_path


@RENDER_SKIP
def test_chain_fix_round_task_text_carries_diagnostics(tmp_path):
    """修正轮任务文本必须包含真实诊断（评审 #1 回归：state 不更新 = 盲重试）。"""
    prof = _profile()
    seen_tasks: list[str] = []

    def factory(system_prompt: str, tools: list):
        tools_by_name = {t.__name__: t for t in tools}

        class _Agent:
            async def run(self, task: str = "") -> None:
                seen_tasks.append(task)
                if len(seen_tasks) == 1:
                    # 缺「订单」关键词 → 诊断不达标
                    body = ('<section class="sec"><h2>行程总览</h2><p>两日。</p></section>'
                            '<section class="sec"><h2>预算一览</h2><p>合计。</p></section>'
                            "<!--TRIPMATE-END-->")
                    await tools_by_name["write_html"](body)
                    await tools_by_name["render_pdf"]()
                else:
                    await tools_by_name["write_html"](_valid_fragment(prof))
                    await tools_by_name["render_pdf"]()
        return _Agent()

    result = _run(designer_chain(prof, "run_diag", factory, system_prompt="sys",
                                 budget_check=lambda: None))
    assert result.attempts == 2
    assert len(seen_tasks) == 2
    fix_task = seen_tasks[1]
    assert "上一版 HTML 未通过渲染诊断" in fix_task
    assert "missing_keywords" in fix_task and "订单" in fix_task  # 真实诊断内容，不是空 dict


def test_chain_raises_after_budget_exhausted(tmp_path):
    prof = _profile()
    script = _factory({"default": [("write_html", "<p>缺关键词与哨兵都不合格</p>"), "render_pdf"]})
    with pytest.raises(DesignerError):
        _run(designer_chain(prof, "run_fail", script, system_prompt="sys",
                            budget_check=lambda: None))


def test_chain_agent_exception_consumes_budget_then_raises():
    prof = _profile()

    def factory(system_prompt: str, tools: list):
        class _Boom:
            async def run(self, task: str = ""):
                raise RuntimeError("模型通道爆炸")
        return _Boom()

    with pytest.raises(DesignerError):
        _run(designer_chain(prof, "run_boom", factory, system_prompt="sys",
                            budget_check=lambda: None))


def test_chain_budget_exceeded_falls_back_to_error():
    prof = _profile()
    exceeded = {"n": 0}

    def budget():
        exceeded["n"] += 1
        raise RuntimeError("token 消耗已超上限")

    with pytest.raises(RuntimeError, match="超上限"):
        _run(designer_chain(_profile(), "run_budget", _factory({"default": []}),
                            system_prompt="sys", budget_check=budget))
    assert exceeded["n"] == 1  # 首轮即熔断，不进循环


def test_chain_cache_hit_reuses_html_without_llm(tmp_path):
    prof = _profile()
    cache_dir = tmp_path / "htmlcache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{designer_cache_key(prof)}.html"
    cached.write_text("<!DOCTYPE html><html><body><p>行程总览</p><p>预算一览</p>"
                      "<p>推荐订单</p></body></html>", encoding="utf-8")
    if not _RENDER_AVAILABLE:
        cached.unlink(missing_ok=True)
        pytest.skip("本机无 Chromium/Edge 内核")
    factory = _factory({})  # 不该被调用
    result = _run(designer_chain(prof, "run_cache", factory, system_prompt="sys",
                                 budget_check=lambda: None))
    assert result.from_cache is True
    assert factory.calls["n"] == 0  # 缓存命中不重新生成
    cached.unlink(missing_ok=True)


def _run(coro):
    import asyncio
    return asyncio.run(coro)
