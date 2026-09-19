"""HTML 路书渲染测试（出片之旅主路径）：冒烟 / 敌意外部文本 / 极端画像边界。

引擎（Playwright Chromium / 系统 Edge）不可用时整模块 skip——此时生产路径会自动
降级 reportlab cartoon，由 test_pdf / test_templates 守护。"""

import pytest

from test_pdf import _profile_bb  # 复用画像构造（tests 目录在 sys.path）

RICH_KWARGS = {}


def _photo_like_cover():
    """封面夹具图：无文字的暖色渐变城市天际线（模拟真实城市宣传照片）。
    不用 generate_placeholder——其烙印的巨型占位文字满版放大后压住封面标题，
    与生产环境（真实照片）的表现完全不符。落盘到 imagegen 的图片目录（测试期被
    conftest 隔离到临时目录）。"""
    from PIL import Image, ImageDraw

    from tripmate.tools.imagegen import IMAGE_DIR
    path = IMAGE_DIR / "cover_fixture_skyline.jpg"
    im = Image.new("RGB", (1600, 1200))
    dr = ImageDraw.Draw(im)
    for y in range(1200):  # 黄昏暖色天空渐变
        t = y / 1200
        dr.line([(0, y), (1600, y)],
                fill=(int(52 + 96 * t), int(40 + 52 * t), int(64 + 26 * (1 - t))))
    dr.rectangle([0, 920, 1600, 1200], fill=(28, 20, 24))  # 地平线剪影
    for x, w, h in [(80, 130, 320), (280, 90, 430), (450, 150, 270), (690, 110, 390),
                    (880, 170, 310), (1130, 100, 450), (1310, 160, 290)]:
        dr.rectangle([x, 920 - h, x + w, 920], fill=(22, 15, 19))
    im.save(path, quality=88)
    return str(path)


def _rich_profile():
    """富画像：天气 + 景点/美食笔记 + 封面宣传图 + 三家酒店（含图）+ 路线 + 锦囊，
    对齐真实定稿数据面（触发速览/闹钟/甘特/导航链接等全部新版式分支）。"""
    from tripmate.models import (AlarmItem, DayTheme, FoodNote, GuideExtras, HotelCandidate,
                                 RouteDay, RouteSegment, RouteStop, SpotNote)
    from tripmate.tools.imagegen import generate_placeholder

    bb = _profile_bb()
    bb.profile.weather = {"city": "成都", "source": "Open-Meteo（真实预报）", "reference_only": False, "days": [
        {"date": "2026-10-01", "day_text": "多云转晴", "temp_min": 20, "temp_max": 31},
        {"date": "2026-10-02", "day_text": "中雨", "temp_min": 19, "temp_max": 26}]}
    bb.profile.spot_notes = [
        SpotNote(name="大熊猫繁育研究基地", intro="成都名片，近距离看大熊猫幼崽", activities="赶早开园即入，优先看月亮产房"),
        SpotNote(name="宽窄巷子", intro="清代川西民居街区", activities="盖碗茶、掏耳、逛文创小店")]
    bb.profile.food_notes = [
        FoodNote(name="火锅", intro="牛油九宫格锅底，毛肚黄喉七上八下", image_path=generate_placeholder("火锅")),
        FoodNote(name="串串香", intro="竹签串菜红汤涮煮，按签计数", image_path=generate_placeholder("串串香"))]
    bb.profile.cover_images = [_photo_like_cover()]
    bb.profile.hotels[0].image_path = generate_placeholder("全季酒店成都春熙路店")
    from tripmate.models import HotelCandidate
    for i, (n, p) in enumerate([("亚朵酒店（天府广场店）", 488), ("如家精选（春熙路店）", 319)], 2):
        bb.profile.hotels.append(HotelCandidate(
            name=n, price_per_night=p, distance_km=1.0, rating=4.4,
            link="https://hotels.ctrip.com/x", selected=False,
            reason="评分次优备选", source="模拟酒店库", reference_only=True,
            image_path=generate_placeholder(n)))
    # 每日路线（含高德 nav_url → 验证导航链接注解）
    stop = RouteStop(kind="spot", name="大熊猫繁育研究基地", address="成都外北熊猫大道",
                     lon=104.144, lat=30.739,
                     nav_url="https://uri.amap.com/navigation?to=104.144,30.739,大熊猫基地&mode=car&src=tripmate")
    stop2 = RouteStop(kind="meal", meal="午餐", name="宽窄巷子小吃", address="青羊区宽窄巷子",
                      lon=104.053, lat=30.663, reference_only=True, note="坐标为参考估算")
    seg = RouteSegment(distance_m=8200, duration_min=25, mode="驾车", reference_only=True)
    bb.profile.routes = [RouteDay(date="2026-10-01", stops=[stop, stop2], segments=[seg],
                                  summary="动线顺，避免折返", total_km=8.2)]
    # 路书锦囊（LLM 提炼产物的结构化形态）
    bb.profile.guide_extras = GuideExtras(
        overview_intro="把熊猫基地放在第一批入园时段，避开十点后的旅行团洪峰；午后转场市区核心，晚间留白。",
        day_themes=[DayTheme(theme="国宝与老街", line="熊猫基地 → 宽窄巷子 → 小吃晚餐")],
        alarms=[AlarmItem(when="9月17日 08:00", action="12306 抢 G1974 车票", channel="开售即抢；候补同步提交",
                          difficulty="🔴 紧俏")],
        hotel_verdict="全季距地铁与春熙路步行圈均衡，品质价格比最优。",
        rhythm_note="第一天只排一个核心点，落地日不赶路。")
    return bb


@pytest.fixture(scope="module")
def html_engine():
    """实测浏览器可启动才跑本模块；不可用 skip（生产路径走 reportlab 降级）。"""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    try:
        pw = sync_playwright().start()
        browser = None
        try:
            try:
                browser = pw.chromium.launch(headless=True, timeout=60_000)
            except Exception:  # noqa: BLE001 — 缺二进制时改系统 Edge
                browser = pw.chromium.launch(headless=True, channel="msedge", timeout=60_000)
        finally:
            if browser is not None:
                browser.close()
            pw.stop()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"HTML 渲染引擎不可用：{type(exc).__name__}: {exc}")


def _render(bb) -> tuple[str, "pymupdf.Document"]:  # noqa: F821
    from tripmate.pdf_html import render
    path = render(bb.profile, run_id="htmltest1")
    import pymupdf
    return path, pymupdf.open(path)


def test_html_pdf_smoke(html_engine):
    import pymupdf
    path, doc = _render(_rich_profile())
    try:
        assert Path_bytes_head(path)
        assert doc.page_count >= 3, "至少封面+正文+封底"
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        assert "成都" in text and "出发前 90 秒速览" in text and "预算总盘" in text
        assert "抢约闹钟日历" in text and "住宿三选一定稿" in text, "新版式章节已渲染"
        assert f"{doc.page_count} / {doc.page_count}" in text, "页码已盖（含总页数）"
        assert doc.get_toc(), "书签目录已生成"
        # 高德导航链接：路线站点 nav_url 必须以可点击链接注解形式存在（删二维码未丢导航）
        links = [lk for i in range(doc.page_count) for lk in doc[i].get_links()
                 if lk.get("kind") == pymupdf.LINK_URI]
        assert any("uri.amap.com" in (lk.get("uri") or "") for lk in links), \
            "路线站点的高德导航直达链接已生成"
    finally:
        doc.close()
        _cleanup(path)


def Path_bytes_head(path: str) -> bool:
    from pathlib import Path as _P
    data = _P(path).read_bytes()
    return data[:4] == b"%PDF" and len(data) > 20000


def test_html_pdf_hostile_external_text(html_engine):
    """回归：LLM/外部文本含 & < > " 时 HTML 路径不炸、不产生注入（autoescape）。"""
    from tripmate.models import (Draft, DraftDay, HotelCandidate, TicketCandidate,
                                 TravelProfile)
    prof = TravelProfile(
        basic_info={"destination": '西<安>&站', "origin": "上海&A", "days": 2,
                    "travel_dates": ["2026-10-01", "2026-10-02"], "style": ["亲子&美食"]},
        tickets=[TicketCandidate(train_no="G1&2<快>", depart_time="08:00", arrive_time="10:00",
                                 duration_min=120, price=100.0, link="https://x?a=1&b=2",
                                 selected=True, reason='靠窗&便宜<"前排"')],
        hotels=[HotelCandidate(name='A&B 酒店<连锁>', price_per_night=300.0, distance_km=0.5,
                               rating=4.5, link="javascript:alert(1)", selected=True,
                               reason='近地铁&含早"双人间"')],
        draft=Draft(days=[DraftDay(date="2026-10-01", morning="兵马俑&华清池",
                                   afternoon="城墙<a>", evening="回民街<b>",
                                   spots=["兵马俑&华清池"])],
                    warnings=["预算紧张&需注意"]),
    )
    path, doc = _render(_as_bb(prof))
    try:
        assert doc.page_count >= 3
    finally:
        doc.close()
        _cleanup(path)


def test_html_pdf_edge_profiles(html_engine):
    """极端画像：无图片/无订单/单日/超预算/无路线/无天气/无笔记，渲染不抛错。"""
    bb = _profile_bb()
    p = bb.profile
    p.images = []
    p.tickets = []
    p.hotels = []
    p.guide_digest = []
    p.weather = {}
    p.routes = []
    from tripmate.models import BasicInfo, Draft, DraftDay
    p.basic_info = BasicInfo(origin="上海", destination="成都", days=1,
                             travel_dates=["2026-10-01"], budget=100, budget_max=80)
    p.draft = Draft(days=[DraftDay(date="2026-10-01", morning="宽窄巷子",
                                   afternoon="锦里", evening="—",
                                   spots=["宽窄巷子"])])
    path, doc = _render(bb)
    try:
        assert doc.page_count >= 3
    finally:
        doc.close()
        _cleanup(path)


def test_html_pdf_output_naming(html_engine):
    """成品命名与 reportlab 路径同规则：outputs/行程计划_{目的地}_{run_id 前 8 位}.pdf。"""
    from pathlib import Path
    bb = _rich_profile()
    path, doc = _render(bb)
    try:
        assert Path(path).parent.name == "outputs"
        assert Path(path).name.startswith("行程计划_成都_")
        assert "htmltes" in Path(path).name
    finally:
        doc.close()
        _cleanup(path)


def _as_bb(prof):
    """裸 TravelProfile → 伪 bb（仅 .profile 属性，供复用 _render）。"""
    class _BB:
        pass
    bb = _BB()
    bb.profile = prof
    return bb


def _cleanup(path: str) -> None:
    from pathlib import Path as _P
    try:
        _P(path).unlink()
    except OSError:
        pass
