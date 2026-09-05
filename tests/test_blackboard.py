"""共享黑板单测：版本号递增、changelog、写入串行化、用户变更检索（§3.6）。"""

import asyncio

from tripmate.blackboard import Blackboard
from tripmate.models import BasicInfo, GuideDigestItem


def run(coro):
    return asyncio.run(coro)


def test_version_increments_and_changelog():
    bb = Blackboard()
    assert bb.version() == 0
    v1 = run(bb.write("guide_digest", [GuideDigestItem(
        source_name="小红书", source_url="https://x", fetched_at="2026-08-28 10:00")],
        "researcher", "攻略结果"))
    assert v1 == 1
    v2 = run(bb.apply_basic_info({"origin": "上海"}, "chatter", "用户输入"))
    assert v2 == 2
    log = bb.profile.changelog
    assert log[0].section == "guide_digest" and log[0].writer == "researcher"
    assert log[1].field == "origin" and log[1].old is None and log[1].new == "上海"


def test_user_changes_since_filters_chatter_only():
    bb = Blackboard()
    run(bb.apply_basic_info({"origin": "上海"}, "chatter", "输入"))
    base = bb.version()
    run(bb.write("tickets", [], "booking", "查询"))            # 团队写入不算用户变更
    assert bb.user_changes_since(base) == []
    run(bb.apply_basic_info({"budget": 5000}, "chatter", "用户改预算"))
    changes = bb.user_changes_since(base)
    assert len(changes) == 1 and changes[0].field == "budget"


def test_apply_detail_hotel_nested():
    bb = Blackboard()
    run(bb.apply_detail_info({"hotel": {"price_range": [300, 500]}}, "chatter", "偏好"))
    assert bb.profile.detail_info.hotel.price_range == [300, 500]
    fields = [e.field for e in bb.profile.changelog]
    assert "hotel.price_range" in fields


def test_missing_required():
    b = BasicInfo()
    assert b.missing_required() == ["出发地", "目的地", "游玩天数"]
    b.origin, b.destination, b.days = "上海", "成都", 3
    assert b.missing_required() == []


def test_changed_fields_chain_hotel_impact_end_to_end():
    """端到端回归（2026-09-05 修复）：apply_detail_info 写入的 hotel.* 变更，经
    user_changes_since → _changed_fields → analyze_impact 必须命中 {"hotels"}——
    此前 _changed_fields 拼了 "detail_info." 前缀导致永远查不中 FIELD_IMPACT。"""
    from tripmate.planning import analyze_impact
    from tripmate.team import _changed_fields

    bb = Blackboard()
    run(bb.apply_basic_info({"origin": "上海"}, "chatter", "输入"))
    base = bb.version()
    run(bb.apply_detail_info({"hotel": {"price_range": [300, 500]}}, "chatter", "改酒店偏好"))
    run(bb.apply_basic_info({"defaults_applied": ["出行方式默认高铁"]}, "chatter", "默认值"))
    fields = _changed_fields(bb.user_changes_since(base))
    assert fields == ["hotel.price_range"]
    assert analyze_impact(fields) == {"hotels"}


def test_changed_fields_party_size_and_days_impact():
    from tripmate.planning import analyze_impact
    from tripmate.team import _changed_fields

    bb = Blackboard()
    base = bb.version()
    run(bb.apply_detail_info({"party_size": 3}, "chatter", "改人数"))
    run(bb.apply_basic_info({"days": 4}, "chatter", "改天数"))
    fields = _changed_fields(bb.user_changes_since(base))
    assert "party_size" in fields and "days" in fields
    affected = analyze_impact(fields)
    assert {"tickets", "hotels", "weather", "itinerary"} <= affected
