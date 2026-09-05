"""全模板冒烟测试：注册表中每个模板都能渲染同一画像（含极端画像），输出合法 PDF。"""

from pathlib import Path

import pytest

from test_pdf import _profile_bb
from tripmate.models import BasicInfo, Draft, DraftDay
from tripmate.pdf_templates import REGISTRY, get_template, list_templates

TEMPLATE_NAMES = sorted(REGISTRY)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_render_smoke(name):
    bb = _profile_bb()
    path = REGISTRY[name].render(bb.profile, run_id=f"tpl_{name}")
    data = Path(path).read_bytes()
    assert data[:4] == b"%PDF" and len(data) > 8000


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_edge_profile(name):
    """极端画像：无图片/无订单/单日行程/超预算，所有模板渲染不抛错。"""
    bb = _profile_bb()
    p = bb.profile
    p.images = []
    p.tickets = []
    p.hotels = []
    p.guide_digest = []
    p.weather = {}
    p.basic_info = BasicInfo(origin="上海", destination="成都", days=1,
                             travel_dates=["2026-10-01"], budget=100, budget_max=120)
    p.draft = Draft(days=[DraftDay(date="2026-10-01", morning="宽窄巷子",
                                   afternoon="锦里", evening="—",
                                   spots=["宽窄巷子"])])
    path = REGISTRY[name].render(p, run_id=f"edge_{name}")
    data = Path(path).read_bytes()
    assert data[:4] == b"%PDF" and len(data) > 4000


def test_registry_metadata_and_errors():
    metas = list_templates()
    assert metas and all(m["name"] and m["display_name"] for m in metas)
    assert get_template(None).name == "cartoon"
    with pytest.raises(ValueError):
        get_template("no_such_template")


def test_all_templates_survive_hostile_external_text():
    """回归（2026-09-05 修复 #14）：外部/LLM 文本含 & < > " 时全部 7 模板仍能出 PDF。

    此前 reportlab paraparser 对裸 &/< 直接抛异常——一个 "A&B Hotel" 就让整个
    finalize 阶段报废。转义必须既防漏（渲染成功）又防双转义（文本原样保留）。"""
    from tripmate.models import (Draft, DraftDay, HotelCandidate, TicketCandidate,
                                 TravelProfile)
    from tripmate.pdf_gen import build_pdf

    prof = TravelProfile(
        basic_info={"destination": '西<安>&站', "origin": "上海&A", "days": 2,
                    "travel_dates": ["2026-10-01", "2026-10-02"], "style": ["亲子&美食"]},
        tickets=[TicketCandidate(train_no="G1&2<快>", depart_time="08:00", arrive_time="10:00",
                                 duration_min=120, price=100.0, link="https://x?a=1&b=2",
                                 selected=True, reason='靠窗&便宜<"前排"')],
        hotels=[HotelCandidate(name='A&B 酒店<连锁>', price_per_night=300.0, distance_km=0.5,
                               rating=4.5, link="https://h?a=1&b=2", selected=True,
                               reason='近地铁&含早"双人间"')],
        draft=Draft(days=[DraftDay(date="2026-10-01", morning="兵马俑&华清池",
                                   afternoon="城墙<a>", evening="回民街<b>",
                                   spots=["兵马俑&华清池"])],
                    warnings=["预算紧张&需注意"]),
    )
    from tripmate.pdf_templates import list_templates
    for t in list_templates():
        path = build_pdf(prof.model_copy(deep=True), f"hostile{t['name']}", t["name"])
        assert path
