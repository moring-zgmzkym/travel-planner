"""定稿分流与回退链单测（docs/html-designer-plan.md 任务 3.1 验收）：

- template="designer" + Designer 崩溃 → 自动回退模板 PDF（用户永远能拿到 PDF）
- Designer 挂起超时 → 同样回退
- CancelledError（用户停止）→ 原样穿透，不转回退
- 模板名过滤：非法值不进注册表，build_pdf 不炸
- 渲染来源标注 render_source 正确

以 autogen stub 导入 team（有真 autogen 时直接用真实模块）。
"""

import asyncio
import json
from pathlib import Path

import pytest

try:
    import tripmate.team  # noqa: F401 — 有真 autogen 的环境
except ImportError:
    from autogen_stub import stub_autogen
    stub_autogen()

from tripmate.blackboard import Blackboard
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, FinalDelivery,
                             HotelCandidate, TicketCandidate, TravelProfile)
from tripmate.status import StatusBus
from tripmate.team import TeamContext, TeamState, _deliver_final


def _profile(template: str | None = "designer") -> TravelProfile:
    prof = TravelProfile()
    prof.basic_info = BasicInfo(origin="上海", destination="成都", days=2, travel_mode="高铁",
                                travel_dates=["2026-10-01", "2026-10-02"], budget=5000,
                                party_size=2, template=template)
    prof.detail_info = DetailInfo(party_size=2)
    prof.tickets = [TicketCandidate(train_no="D636", depart_time="09:15", arrive_time="22:40",
                                    duration_min=805, price=609.0, link="https://kyfw.12306.cn",
                                    selected=True, reference_only=True)]
    prof.hotels = [HotelCandidate(name="亚朵酒店", price_per_night=488.0, distance_km=0.6,
                                  rating=4.8, link="https://hotels.ctrip.com", selected=True)]
    prof.draft = Draft(days=[
        DraftDay(date="2026-10-01", morning="乘 D636 赴蓉", afternoon="入住春熙路",
                 evening="太古里夜景", spots=["春熙路太古里"]),
        DraftDay(date="2026-10-02", morning="大熊猫基地", afternoon="宽窄巷子",
                 evening="返程", spots=["大熊猫基地", "宽窄巷子"]),
    ])
    return prof


def _ctx(bb: Blackboard) -> TeamContext:
    bus = StatusBus()
    return TeamContext(bb=bb, bus=bus, state=TeamState(), jobs=None,  # type: ignore[arg-type]
                       runner=None, run_id="fbtest01")  # type: ignore[arg-type]


def _fill(bb: Blackboard, template: str | None) -> None:
    """Blackboard.profile 为只读属性，按 e2e_step4 先例逐字段填充。"""
    src = _profile(template)
    bb.profile.basic_info = src.basic_info
    bb.profile.detail_info = src.detail_info
    bb.profile.tickets = src.tickets
    bb.profile.hotels = src.hotels
    bb.profile.draft = src.draft


def _run(coro):
    return asyncio.run(coro)


def test_designer_crash_falls_back_to_template_pdf(tmp_path, monkeypatch):
    from tripmate import designer as dmod

    bb = Blackboard()
    _fill(bb, "designer")
    ctx = _ctx(bb)

    async def _boom(**kwargs):
        raise RuntimeError("Playwright 崩溃")

    monkeypatch.setattr(dmod, "designer_chain", _boom)
    result = json.loads(_run(_deliver_final(ctx)))
    assert result["status"] == "ok"
    assert result["render_source"].startswith("template:")
    pdf = Path(result["pdf_path"])
    assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"
    final: FinalDelivery = bb.profile.final
    assert final is not None and final.render_source == result["render_source"]
    assert ctx.state.step == "DONE"


def test_designer_timeout_falls_back_to_template_pdf(tmp_path, monkeypatch):
    """挂起（而非崩溃）也必须回退——asyncio.wait_for 熔断路径。"""
    from tripmate import designer as dmod

    bb = Blackboard()
    _fill(bb, "designer")
    ctx = _ctx(bb)

    async def _hang(**kwargs):
        await asyncio.sleep(999)

    monkeypatch.setattr(dmod, "designer_chain", _hang)
    monkeypatch.setattr(dmod, "DESIGNER_TIMEOUT_S", 0.05)  # 快速熔断
    result = json.loads(_run(_deliver_final(ctx)))
    assert result["status"] == "ok"
    assert result["render_source"].startswith("template:")
    assert Path(result["pdf_path"]).exists()


def test_cancelled_error_propagates_no_fallback(tmp_path, monkeypatch):
    """用户停止：CancelledError 穿透，不回退不吞（停止就是停止）。"""
    from tripmate import designer as dmod

    bb = Blackboard()
    _fill(bb, "designer")
    ctx = _ctx(bb)

    async def _cancelled(**kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dmod, "designer_chain", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        _run(_deliver_final(ctx))
    assert bb.profile.final is None


def test_designer_success_path(tmp_path, monkeypatch):
    """designer 链成功：render_source=designer，final 落盘。"""
    from tripmate import designer as dmod

    bb = Blackboard()
    _fill(bb, "designer")
    ctx = _ctx(bb)
    fake_pdf = tmp_path / "fake_designer.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake for test")

    async def _ok(**kwargs):
        return dmod.DesignerResult(pdf_path=str(fake_pdf), html_path="x.html",
                                   engine="playwright", attempts=1)

    monkeypatch.setattr(dmod, "designer_chain", _ok)
    result = json.loads(_run(_deliver_final(ctx)))
    assert result["status"] == "ok" and result["render_source"] == "designer"
    assert bb.profile.final.render_source == "designer"


def test_illegal_template_value_never_reaches_registry(tmp_path):
    """template 为损坏值（非 designer 也非注册表名）→ 过滤为 None，回退 classic 不炸。"""
    bb = Blackboard()
    _fill(bb, "templ-not-exists")
    ctx = _ctx(bb)
    result = json.loads(_run(_deliver_final(ctx)))
    assert result["status"] == "ok"
    assert result["render_source"] == "template:classic"
