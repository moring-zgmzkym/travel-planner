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
