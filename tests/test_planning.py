"""规划纯逻辑单测：变更影响分析（§5.3）、草稿校验、预算核算（§4.5）、订单打分（§4.4）。"""

from tripmate.blackboard import Blackboard
from tripmate.models import Draft, DraftDay, HotelCandidate, TicketCandidate
from tripmate.planning import analyze_impact, compute_budget, validate_draft


def _profile_bb() -> Blackboard:
    bb = Blackboard()
    from tripmate.models import BasicInfo, DetailInfo, HotelPref
    bb.profile.basic_info = BasicInfo(
        origin="上海", destination="成都", days=3,
        travel_dates=["2026-10-01", "2026-10-02", "2026-10-03"],
        travel_mode="高铁", style=["休闲"], budget=6000, budget_max=7000, party_size=2)
    bb.profile.detail_info = DetailInfo(
        hotel=HotelPref(location_pref="春熙路", price_range=[300, 500]),
        must_visit=["大熊猫繁育研究基地"], pace="中", party_size=2)
    bb.profile.tickets = [TicketCandidate(
        train_no="G1974", depart_time="06:58", arrive_time="19:26", duration_min=748,
        price=926.5, link="https://12306", score=0.9, selected=True, reason="评分最高")]
    bb.profile.hotels = [HotelCandidate(
        name="全季酒店", price_per_night=429, distance_km=0.4, rating=4.7,
        link="https://ctrip", score=0.9, selected=True, reason="评分最高")]
    return bb


def _draft(days=3, spots_per_day=3, must=True) -> Draft:
    dd = []
    for i in range(days):
        spots = []
        if must and i == 0:
            spots.append("大熊猫繁育研究基地")
        spots += ["宽窄巷子", "锦里古街", "武侯祠"][: spots_per_day - (1 if must and i == 0 else 0)]
        dd.append(DraftDay(date=f"2026-10-0{i + 1}", morning=f"晨{i}", afternoon=f"午{i}",
                           evening=f"晚{i}", spots=spots))
    return Draft(days=dd)


def test_impact_rules():
    # 改目的地 → 全量重跑
    assert "guides" in analyze_impact(["destination"]) and "hotels" in analyze_impact(["destination"])
    # 改出行日期 → 车票+酒店+天气重查，攻略复用
    a = analyze_impact(["travel_dates"])
    assert {"tickets", "hotels", "weather"} <= a and "guides" not in a
    # 改预算 → 仅酒店重查
    a = analyze_impact(["budget"])
    assert a == {"hotels", "itinerary"}
    # 改风格 → 仅行程重排
    assert analyze_impact(["style"]) == {"itinerary"}


def test_validate_draft():
    bb = _profile_bb()
    assert validate_draft(bb.profile, _draft()) == []
    errs = validate_draft(bb.profile, _draft(must=False))
    assert any("必经景点" in e for e in errs)
    errs = validate_draft(bb.profile, _draft(days=2))
    assert any("天数" in e for e in errs)
    errs = validate_draft(bb.profile, _draft(spots_per_day=1))
    assert any("节奏" in e for e in errs)


def test_compute_budget():
    bb = _profile_bb()
    b = compute_budget(bb.profile, _draft())
    # 交通：G1974 单程 926.5 ×2 ×2 人 = 3706
    assert abs(next(i for i in b["items"] if i["item"] == "交通")["amount"] - 3706.0) < 0.01
    # 住宿：429 × 2 晚 = 858
    assert abs(next(i for i in b["items"] if i["item"] == "住宿")["amount"] - 858.0) < 0.01
    # 门票：熊猫基地 55×2 人 + 武侯祠 50×2 人（其余免费景点）= 210
    assert abs(next(i for i in b["items"] if i["item"] == "门票")["amount"] - 210.0) < 0.01
    assert b["total"] > 5000 and b["occupancy"] is not None


def test_compute_budget_overrun_warning():
    bb = _profile_bb()
    bb.profile.basic_info.budget = 3000
    bb.profile.basic_info.budget_max = 3600
    b = compute_budget(bb.profile, _draft())
    assert any("超出最大预算" in w for w in b["warnings"])


def test_ticket_scoring_golden_window():
    from tripmate.tools.tickets import score_and_select
    cands = [
        {"train_no": "A", "depart_time": "08:00", "arrive_time": "20:00", "duration_min": 720,
         "price": 900, "link": "", "source": "t", "reference_only": False},
        {"train_no": "B", "depart_time": "14:00", "arrive_time": "02:00", "duration_min": 720,
         "price": 900, "link": "", "source": "t", "reference_only": False},
    ]
    ranked = score_and_select(cands)
    assert ranked[0]["train_no"] == "A" and ranked[0]["selected"] is True
    assert ranked[0]["score"] > ranked[1]["score"]


def test_hotel_scoring_pref_range():
    from tripmate.tools.hotels import score_and_select
    cands = [
        {"name": "H1", "price_per_night": 400, "distance_km": 0.5, "rating": 4.8, "link": "", "source": "s", "reference_only": False},
        {"name": "H2", "price_per_night": 900, "distance_km": 0.5, "rating": 4.8, "link": "", "source": "s", "reference_only": False},
    ]
    ranked = score_and_select(cands, [300, 500])
    assert ranked[0]["name"] == "H1" and ranked[0]["selected"] is True


def test_compute_budget_max_only_warning():
    # 只填"最大预算"：超支预警必须生效（修复前 budget=0 时永不触发），occupancy 按上限计算
    bb = _profile_bb()
    bb.profile.basic_info.budget = None
    bb.profile.basic_info.budget_max = 3500
    b = compute_budget(bb.profile, _draft())
    assert any("超出最大预算" in w for w in b["warnings"])
    assert b["budget_max"] == 3500 and b["occupancy"] is not None and b["budget"] == 0


def test_compute_budget_only_90pct_warning():
    # 只填预算：budget_max 自动 ×1.2，超 90% 触发预警但未破上限
    bb = _profile_bb()
    bb.profile.basic_info.budget = 5500
    bb.profile.basic_info.budget_max = None
    b = compute_budget(bb.profile, _draft())
    assert b["budget_max"] == 6600.0
    assert any(">90%" in w for w in b["warnings"])
    assert not any("超出最大预算" in w for w in b["warnings"])
