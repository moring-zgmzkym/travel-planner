"""降级知识库增补与区域别名映射回归（成都→陕南用例内容占位化修复）。"""

from tripmate.chatter import REGION_ALIAS
from tripmate.mocks.data import kb_for_city, spot_ticket_price


def test_shannan_cities_in_kb():
    # 陕南三城已收录：不走通用模板，景点/美食/路线/避坑齐全
    for city in ("汉中", "安康", "商洛"):
        kb = kb_for_city(city)
        assert kb["spots"] and kb["foods"] and kb["routes"] and kb["warnings"]
        assert not kb["spots"][0].endswith("市中心历史街区")  # 通用模板的占位景点名


def test_shannan_ticket_prices_resolvable():
    # 门票估价可命中知识库（compute_budget 依赖），不再一律落到 50 元默认值
    assert spot_ticket_price("汉中石门栈道") == 70
    assert spot_ticket_price("瀛湖风景区") == 100
    assert spot_ticket_price("金丝峡景区") == 100


def test_generic_template_unchanged_for_unknown_city():
    # 未收录城市仍走模板（既有行为不回归）
    kb = kb_for_city("某未知城市")
    assert "某未知城市市中心历史街区" in kb["spots"]


def test_region_alias_exact_match():
    # 仅精确匹配区域词；宽泛词（陕西）绝不自动替换
    assert REGION_ALIAS["陕南"] == "汉中"
    assert REGION_ALIAS["陕西南部"] == "汉中"
    assert "陕西" not in REGION_ALIAS
    assert "西安" not in REGION_ALIAS
