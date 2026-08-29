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
