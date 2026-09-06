"""PDF 生成冒烟测试（reportlab + 中文字体 + 占位图）。"""

from tripmate.blackboard import Blackboard
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, GuideDigestItem,
                             HotelCandidate, HotelPref, ImageItem, TicketCandidate)
from tripmate.pdf_gen import build_pdf
from tripmate.tools.imagegen import generate_placeholder


def _profile_bb() -> Blackboard:
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(
        origin="上海", destination="成都", days=2,
        travel_dates=["2026-10-01", "2026-10-02"], travel_mode="高铁",
        style=["休闲", "美食"], budget=6000, budget_max=7000, party_size=2)
    bb.profile.detail_info = DetailInfo(
        hotel=HotelPref(location_pref="春熙路", price_range=[300, 500]),
        must_visit=["大熊猫繁育研究基地"], pace="中")
    bb.profile.guide_digest = [GuideDigestItem(
        source_name="小红书（搜索摘要级）", source_url="https://xiaohongshu.com/s?kw=成都",
        fetched_at="2026-08-28 10:00", spots=["大熊猫繁育研究基地", "宽窄巷子"],
        foods=["火锅", "串串香"], routes=["D1 熊猫基地→宽窄巷子"],
        warnings=["熊猫基地要早去"], reference_only=True)]
    bb.profile.tickets = [TicketCandidate(
        train_no="G1974", depart_time="06:58", arrive_time="19:26", duration_min=748,
        price=926.5, link="https://kyfw.12306.cn", score=0.92, selected=True,
        reason="出发时间最优", source="模拟班次表", reference_only=True)]
    bb.profile.hotels = [HotelCandidate(
        name="全季酒店（成都春熙路店）", price_per_night=429, distance_km=0.4, rating=4.7,
        link="https://hotels.ctrip.com/x", score=0.9, selected=True,
        reason="价格契合+距离最近", source="模拟酒店库", reference_only=True)]
    bb.profile.images = [ImageItem(spot=s, path=generate_placeholder(s), source="本地示意配图（模拟数据模式）")
                         for s in ("大熊猫繁育研究基地", "宽窄巷子", "锦里古街", "武侯祠",
                                   "都江堰", "春熙路太古里")]
    bb.profile.draft = Draft(days=[
        DraftDay(date="2026-10-01", morning="熊猫基地", afternoon="宽窄巷子", evening="锦里",
                 spots=["大熊猫繁育研究基地", "宽窄巷子", "锦里古街"]),
        DraftDay(date="2026-10-02", morning="都江堰", afternoon="返程", evening="—",
                 spots=["都江堰", "春熙路太古里"]),
    ], budget_total=5510.5, warnings=["总预算已占用 92%（>90% 预警）"])
    return bb


def test_build_pdf_smoke():
    bb = _profile_bb()
    path = build_pdf(bb.profile, run_id="testrun01")
    data = open(path, "rb").read()
    assert data[:4] == b"%PDF" and len(data) > 10000


def _routes_profile_bb() -> Blackboard:
    """含每日路线的画像（本文件与 test_templates 的路线表渲染测试共用）。"""
    from tripmate.models import RouteDay, RouteSegment, RouteStop

    bb = _profile_bb()
    nav = ("https://uri.amap.com/navigation?to=104.14,30.73,%E7%86%8A%E7%8C%AB"
           "&mode=bus&src=tripmate&coordinate=gaode&callnative=0")
    bb.profile.routes = [RouteDay(
        date="2026-10-01",
        stops=[
            RouteStop(kind="spot", name="大熊猫繁育研究基地", lon=104.14, lat=30.73, nav_url=nav),
            RouteStop(kind="meal", meal="午餐", name="火锅店（春熙路店）", address="春熙路1号",
                      lon=104.08, lat=30.66, nav_url=nav),
            RouteStop(kind="spot", name="宽窄巷子", note="坐标未获取"),
        ],
        segments=[RouteSegment(distance_m=8000, duration_min=30, mode="公交"),
                  RouteSegment(distance_m=600, duration_min=9, mode="步行"),
                  RouteSegment()],
        summary="路线整体顺路，午餐紧邻下午首站",
        warnings=["下午两站距离较远（估算）"],
        total_km=8.6)]
    return bb


def test_build_pdf_with_routes_smoke():
    path = build_pdf(_routes_profile_bb().profile, run_id="routerun1")
    data = open(path, "rb").read()
    assert data[:4] == b"%PDF" and len(data) > 10000


# ---- 降级演练（2026-09-06 HTML 主路径改造）：引擎故障 / 应急开关 / 旧模板名兼容 ----

def test_build_pdf_fallback_on_engine_failure(monkeypatch):
    """HTML 引擎抛异常 → 自动降级 reportlab cartoon，on_fallback 回调携原因触发。"""
    import tripmate.pdf_html as pdf_html

    def _boom(profile, run_id):
        raise RuntimeError("engine boom")

    monkeypatch.setattr(pdf_html, "render", _boom)
    fired: list[str] = []
    path = build_pdf(_profile_bb().profile, run_id="fbrun001",
                     on_fallback=fired.append)
    data = open(path, "rb").read()
    assert data[:4] == b"%PDF" and len(data) > 10000
    assert fired and "engine boom" in fired[0]


def test_build_pdf_fallback_callback_exception_swallowed(monkeypatch):
    """on_fallback 回调自身抛异常不影响 PDF 交付。"""
    import tripmate.pdf_html as pdf_html

    monkeypatch.setattr(pdf_html, "render", lambda p, r: (_ for _ in ()).throw(RuntimeError("boom")))
    def _bad_notify(reason):
        raise ValueError("notify failed")

    path = build_pdf(_profile_bb().profile, run_id="fbrun002", on_fallback=_bad_notify)
    assert open(path, "rb").read()[:4] == b"%PDF"


def test_build_pdf_reportlab_switch(monkeypatch):
    """PDF_RENDERER=reportlab：一键回退 reportlab，HTML 引擎不应被触碰。"""
    import tripmate.pdf_html as pdf_html

    def _boom(profile, run_id):
        raise AssertionError("HTML 引擎不应被调用")

    monkeypatch.setattr(pdf_html, "render", _boom)
    monkeypatch.setattr("tripmate.pdf_gen.PDF_RENDERER", "reportlab")
    path = build_pdf(_profile_bb().profile, run_id="switchrun1")
    assert open(path, "rb").read()[:4] == b"%PDF"


def test_render_reportlab_legacy_template_name():
    """存量会话持久化旧模板名不再引发 ValueError（未知名回退默认模板）。"""
    from tripmate.pdf_gen import render_reportlab
    path = render_reportlab(_profile_bb().profile, run_id="legacyrun1", template="classic")
    assert open(path, "rb").read()[:4] == b"%PDF"


def test_deliver_final_fallback_notify(monkeypatch):
    """集成：HTML 引擎炸 → _deliver_final 仍交付 PDF 并向状态总线推送 STATUS_FALLBACK。"""
    import asyncio

    import tripmate.pdf_html as pdf_html
    from tripmate.status import StatusBus
    from tripmate.team import TeamContext, TeamRunner, TeamState, _deliver_final

    def _boom(profile, run_id):
        raise RuntimeError("engine boom")

    monkeypatch.setattr(pdf_html, "render", _boom)
    bb = _profile_bb()
    bus = StatusBus()
    runner = TeamRunner(bb, bus)
    ctx = TeamContext(bb=bb, bus=bus, state=TeamState(), jobs=runner._jobs,
                      runner=runner, run_id="fbevents01")

    async def main():
        q = bus.subscribe()
        result = await _deliver_final(ctx)
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        return result, evs

    _, events = asyncio.run(main())
    final = bb.profile.final
    assert final is not None and final.pdf_path
    assert open(final.pdf_path, "rb").read()[:4] == b"%PDF"
    assert any(e["kind"] == "STATUS_FALLBACK" for e in events), "降级时间线提示未推送"
    assert any(e["kind"] == "STATUS_COMPLETED" for e in events), "完成事件仍应推送"
