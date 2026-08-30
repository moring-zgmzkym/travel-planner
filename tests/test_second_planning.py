"""二次规划修复回归（2026-08-30）：缺字段闸门 / 启动意图正则 / start() 黑板清理。

全程 stub：stream_chatter 返回固定回复、_phase_loop 置空——不联网、不真跑团队。
"""

import asyncio

import pytest

from tripmate.blackboard import Blackboard
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, GuideDigestItem,
                             HotelCandidate, ImageItem, TicketCandidate)
from tripmate.session import Session, _START_INTENT
from tripmate.status import StatusBus
from tripmate.team import TeamRunner


def _bb(destination: str, with_origin: bool = True) -> Blackboard:
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(
        origin="上海" if with_origin else "", destination=destination, days=3,
        travel_dates=["2026-10-01", "2026-10-03"], travel_mode="高铁", party_size=2)
    bb.profile.detail_info = DetailInfo(must_visit=["大熊猫基地"], party_size=2)
    return bb


def _stale_run(bb: Blackboard, destination: str) -> None:
    """构造上一轮（destination）定稿后的全部残留。"""
    bb.profile.guide_digest = [GuideDigestItem(
        source_name="旧来源", source_url="https://old.example.com", fetched_at="2026-08-30 10:00",
        spots=[f"{destination}旧景点"], foods=[], routes=[], warnings=[], reference_only=True)]
    bb.profile.tickets = [TicketCandidate(
        train_no="G9999", depart_time="08:00", arrive_time="12:00", duration_min=240,
        price=100.0, link="https://kyfw.12306.cn", score=0.5, selected=True,
        reason="旧行程车票", source="模拟", reference_only=True)]
    bb.profile.hotels = [HotelCandidate(
        name=f"{destination}旧酒店", price_per_night=300, distance_km=1.0, rating=4.5,
        link="https://hotels.ctrip.com", score=0.5, selected=True,
        reason="旧行程酒店", source="模拟", reference_only=True)]
    bb.profile.weather = {"days": [{"date": "2026-10-01", "day_text": "晴"}], "source": "模拟"}
    bb.profile.images = [ImageItem(spot=f"{destination}旧景点", path="x.png", source="旧图")]
    bb.profile.draft = Draft(days=[DraftDay(
        date="2026-10-01", morning=f"{destination}上午", afternoon=f"{destination}下午",
        evening=f"{destination}晚上", spots=[f"{destination}景点"])])
    bb.profile.draft_feedback = None


def _make_session(bb: Blackboard, monkeypatch, chatter_reply: str) -> Session:
    """构造绕过真实 LLM 的 Session：stream_chatter 返回固定回复，团队阶段循环置空。"""

    async def fake_stream(chatter, text, source="user"):
        return chatter_reply

    async def fake_phase(self, phase):
        return None

    monkeypatch.setattr("tripmate.session.stream_chatter", fake_stream)
    monkeypatch.setattr(TeamRunner, "_phase_loop", fake_phase)
    s = Session.__new__(Session)
    s.bb = bb
    s.bus = StatusBus()
    s.team_events = asyncio.Queue()
    s.runner = TeamRunner(bb, s.bus)
    s.chatter = object()  # stub 不使用
    s.chatter_lock = asyncio.Lock()
    return s


# ---- 正则 ----

def test_start_intent_regex_variants():
    assert _START_INTENT.search("现在为您启动新一轮规划。")
    assert _START_INTENT.search("信息已齐备，立即启动规划团队")
    assert _START_INTENT.search("好的，开始规划")
    assert _START_INTENT.search("start_planning")
    assert _START_INTENT.search("规划团队已在后台启动")
    assert not _START_INTENT.search("请问您从哪里出发呢？")  # 普通追问不误命中


# ---- 缺字段闸门（问题 2 回归）----

def test_missing_origin_keeps_reply_and_never_starts(monkeypatch):
    """信息不全时回复含"启动规划"：追问原样保留，绝不触发确定性启动（2026-08-30 事故回归）。"""
    bb = _bb("汉中", with_origin=False)
    ask = "请问您从哪里出发呢？补充出发地后我立即为您启动规划。"
    s = _make_session(bb, monkeypatch, ask)
    reply = asyncio.run(s.handle_user_message("帮我规划汉中3天"))
    assert reply == ask  # 追问原文原样返回，未被"启动规划未成功：…"覆盖
    assert not s.runner.active and s.runner._task is None  # 团队未启动


def test_complete_profile_deterministic_start_fires(monkeypatch):
    """信息齐备 + 回复宣称"启动新一轮规划"（工具未调用的实测漏接变体）→ 确定性补启动。"""
    bb = _bb("汉中")
    claim = "好的，现在为您启动新一轮规划。"
    s = _make_session(bb, monkeypatch, claim)
    reply = asyncio.run(s.handle_user_message("帮我规划汉中3天"))
    assert reply.startswith("信息已齐备，旅行规划团队已在后台启动")
    assert s.runner.active  # 确定性启动已受理


# ---- start() 黑板清理（问题 1 回归）----

def test_start_clears_stale_run_same_destination(monkeypatch):
    """同目的地二次规划：草稿/成品/反馈必清；数据分区保留（未换目的地）。"""
    bb = _bb("成都")
    _stale_run(bb, "成都")
    s = _make_session(bb, monkeypatch, "r")
    s.runner._last_destination = "成都"

    async def main():
        receipt = s.runner.start()
        await asyncio.sleep(0)  # 让 stub 阶段任务跑完
        return receipt

    receipt = asyncio.run(main())
    assert receipt["status"] == "accepted"
    p = s.bb.profile
    assert p.draft is None and p.final is None and p.draft_feedback is None and p.plan_input is None
    assert len(p.tickets) == 1 and len(p.guide_digest) == 1  # 同目的地数据保留


def test_start_clears_data_sections_on_destination_change(monkeypatch):
    """换目的地二次规划：上一轮车票/酒店/攻略/天气/图片全部清空，杜绝旧数据混入新行程。"""
    bb = _bb("汉中")  # 画像已是新目的地
    _stale_run(bb, "淄博")  # 黑板数据仍是上一轮淄博
    s = _make_session(bb, monkeypatch, "r")
    s.runner._last_destination = "淄博"

    async def main():
        receipt = s.runner.start()
        await asyncio.sleep(0)
        return receipt

    receipt = asyncio.run(main())
    assert receipt["status"] == "accepted"
    p = s.bb.profile
    assert p.draft is None and p.final is None
    assert p.tickets == [] and p.hotels == [] and p.guide_digest == []
    assert p.weather == {} and p.images == []
    assert s.runner._last_destination == "汉中"
    assert s.runner._base_version == s.bb.version()  # 基线版本采集自清空后的黑板
