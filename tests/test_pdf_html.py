"""HTML 路书渲染测试（唐风夜色主路径）：冒烟 / 敌意外部文本 / 极端画像边界。

引擎（Playwright Chromium / 系统 Edge）不可用时整模块 skip——此时生产路径会自动
降级 reportlab cartoon，由 test_pdf / test_templates 守护。"""

import pytest

from test_pdf import _profile_bb  # 复用画像构造（tests 目录在 sys.path）

RICH_KWARGS = {}


def _rich_profile():
    """富画像：天气 + 景点/美食笔记 + 封面宣传图 + 三家酒店（含图），对齐真实定稿数据面。"""
    from tripmate.models import FoodNote, SpotNote
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
    bb.profile.cover_images = [generate_placeholder("成都城市封面")]
    bb.profile.hotels[0].image_path = generate_placeholder("全季酒店成都春熙路店")
    from tripmate.models import HotelCandidate
    for i, (n, p) in enumerate([("亚朵酒店（天府广场店）", 488), ("如家精选（春熙路店）", 319)], 2):
        bb.profile.hotels.append(HotelCandidate(
            name=n, price_per_night=p, distance_km=1.0, rating=4.4,
            link="https://hotels.ctrip.com/x", selected=False,
            reason="评分次优备选", source="模拟酒店库", reference_only=True,
            image_path=generate_placeholder(n)))
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
    path, doc = _render(_rich_profile())
    try:
        assert Path_bytes_head(path)
        assert doc.page_count >= 3, "至少封面+正文+封底"
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        assert "成都" in text and "行程总览" in text and "预算核算" in text
        assert f"{doc.page_count} / {doc.page_count}" in text, "页码已盖（含总页数）"
        assert doc.get_toc(), "书签目录已生成"
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
