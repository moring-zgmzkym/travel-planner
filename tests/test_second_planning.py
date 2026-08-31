"""二次规划修复回归（2026-08-30）：缺字段闸门 / 启动意图正则 / start() 黑板清理。

全程 stub：stream_chatter 返回固定回复、_phase_loop 置空——不联网、不真跑团队。
"""

import asyncio

import pytest

from tripmate.blackboard import Blackboard
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, GuideDigestItem,
                             HotelCandidate, ImageItem, TicketCandidate)
from tripmate.session import Session, _FEEDBACK_INTENT, _START_INTENT
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

    async def fake_stream(chatter, text, source="user", seen_tools=None):
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
    # 2026-08-31 实测漏接变体：称呼语插在"开始"与"规划"之间，团队从未启动、面板全程无事件
    assert _START_INTENT.search("已记下您的需求，现在开始为您规划。")
    assert _START_INTENT.search("需求已记好，现在开始为您规划。")
    # 延迟/拒绝语义不得误命中（否则用户明确说"先别开始"也会被兜底启动）
    assert not _START_INTENT.search("等您确认后再开始规划。")
    assert not _START_INTENT.search("先别开始规划，我还没想好。")
    assert not _START_INTENT.search("规划团队还没启动，请稍候。")


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


def test_incident_variant_announce_starts_team(monkeypatch):
    """2026-08-31 事故回归：回复宣布"现在开始为您规划"（称呼语插入变体）但工具未执行 → 确定性补启动。"""
    bb = _bb("汉中")
    s = _make_session(bb, monkeypatch, "已记下您的需求，现在开始为您规划。")
    reply = asyncio.run(s.handle_user_message("帮我规划汉中3天"))
    assert reply.startswith("信息已齐备，旅行规划团队已在后台启动")
    assert s.runner.active


def test_tool_actually_executed_skips_fallback(monkeypatch):
    """工具本轮已真正执行（start_planning 出现在执行事件中）：回执即启动确认，兜底绝不二次干预。"""
    bb = _bb("汉中")

    async def fake_stream(chatter, text, source="user", seen_tools=None):
        if seen_tools is not None:
            seen_tools.add("start_planning")
        return "已启动旅行规划团队（后台运行，您可继续补充信息）"

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
    reply = asyncio.run(s.handle_user_message("帮我规划汉中3天"))
    assert reply == "已启动旅行规划团队（后台运行，您可继续补充信息）"  # 原样放行，未被改写
    assert not s.runner.active  # 未被兜底二次启动


# ---- 反馈提交兜底（2026-08-31 完整流程实测：宣布已转交但工具未执行 → 修订流程卡死）----

def test_feedback_intent_regex():
    assert _FEEDBACK_INTENT.search("已记下您的偏好，现在把这条修改意见转给规划团队。")
    assert _FEEDBACK_INTENT.search("这是对草稿的修改意见，我来提交给规划团队。")  # 第二种实测变体
    assert _FEEDBACK_INTENT.search("好的，修改意见已提交给规划团队。")
    assert not _FEEDBACK_INTENT.search("请问第 2 天想怎么调整呢？")  # 追问不误命中
    assert not _FEEDBACK_INTENT.search("您的预算已更新，规划会自动体现。")  # 一般性确认不误命中


def test_feedback_announce_nudges_and_recovers(monkeypatch):
    """草稿待反馈期宣布已提交修改意见但工具未执行 → nudge 一轮，第二轮真正提交。"""
    bb = _bb("汉中")
    _stale_run(bb, "汉中")  # 构造草稿待反馈态（draft 存在、feedback 为空）
    calls = {"n": 0}

    async def fake_stream(chatter, text, source="user", seen_tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return "已记下您的偏好，现在把这条修改意见转给规划团队。"  # 宣布但未调工具
        if seen_tools is not None:
            seen_tools.add("submit_draft_feedback")
        return "修改意见已提交给规划团队。"

    async def fake_phase(self, phase):
        return None

    monkeypatch.setattr("tripmate.session.stream_chatter", fake_stream)
    monkeypatch.setattr(TeamRunner, "_phase_loop", fake_phase)
    s = Session.__new__(Session)
    s.bb = bb
    s.bus = StatusBus()
    s.team_events = asyncio.Queue()
    s.runner = TeamRunner(bb, s.bus)
    s.runner._awaiting_feedback = True
    s.chatter = object()  # stub 不使用
    s.chatter_lock = asyncio.Lock()
    reply = asyncio.run(s.handle_user_message("第 2 天换成龙泉古镇"))
    assert reply == "修改意见已提交给规划团队。"  # nudge 后的真实回复
    assert calls["n"] == 2  # nudge 重试确实发生


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
