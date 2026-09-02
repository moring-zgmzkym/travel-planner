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
    assert get_template(None).name == "classic"
    with pytest.raises(ValueError):
        get_template("no_such_template")
