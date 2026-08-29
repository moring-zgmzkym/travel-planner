"""端到端冒烟④（无 LLM）：黑板手工构造草稿状态 → _deliver_final 定稿 → PDF/订单/完成事件验证。

验证 finalize 环节的确定性逻辑（PDF 生成、订单清单组装、final 分区写入、STATUS_COMPLETED），
不消耗 LLM 额度——LLM 协同部分见 e2e_step1/2/3。
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate.blackboard import Blackboard  # noqa: E402
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, GuideDigestItem,  # noqa: E402
                             HotelCandidate, HotelPref, TicketCandidate)
from tripmate.status import StatusBus  # noqa: E402
from tripmate.team import TeamContext, TeamRunner, TeamState, _deliver_final  # noqa: E402
from tripmate.tools.imagegen import generate_placeholder  # noqa: E402


async def main() -> None:
    bb = Blackboard()
    bb.profile.basic_info = BasicInfo(
        origin="上海", destination="成都", days=3,
        travel_dates=["2026-10-01", "2026-10-02", "2026-10-03"],
        travel_mode="高铁", style=["休闲", "美食"], budget=5000, budget_max=7000, party_size=2,
        defaults_applied=["出行时间默认近期"])
    bb.profile.detail_info = DetailInfo(
        hotel=HotelPref(location_pref="春熙路附近", price_range=[300, 500]),
        must_visit=["大熊猫基地"], pace="中", party_size=2)
    bb.profile.guide_digest = [GuideDigestItem(
        source_name="小红书（搜索摘要级）", source_url="https://www.xiaohongshu.com/search_result?keyword=成都攻略",
        fetched_at="2026-08-28 23:30", spots=["大熊猫繁育研究基地", "宽窄巷子", "锦里古街"],
        foods=["火锅", "串串香", "龙抄手"], routes=["D1 熊猫基地→宽窄巷子→锦里"],
        warnings=["熊猫基地一定要早上开园就去"], reference_only=True)]
    bb.profile.tickets = [
        dict(t) for t in [
            {"train_no": "D636", "depart_time": "09:15", "arrive_time": "22:40", "duration_min": 805,
             "price": 609.0, "link": "https://kyfw.12306.cn/otn/leftTicket/init", "score": 0.8,
             "selected": True, "reason": "综合评分最高（0.8）：出发时间黄金窗口，价格最低", "reference_only": True},
        ]
    ]
    from tripmate.models import TicketCandidate, HotelCandidate
    bb.profile.tickets = [TicketCandidate(**t) for t in bb.profile.tickets]
    bb.profile.hotels = [HotelCandidate(
        name="亚朵酒店（成都天府广场店）", price_per_night=488.0, distance_km=0.6, rating=4.8,
        link="https://hotels.ctrip.com/x", score=0.953, selected=True,
        reason="综合评分最高：价格契合 300-500 区间，距地标 0.6km", reference_only=True)]
    bb.profile.draft = Draft(days=[
        DraftDay(date="2026-10-01", morning="乘 D636 高铁赴蓉", afternoon="抵蓉入住春熙路",
                 evening="春熙路太古里夜景", spots=["春熙路太古里"]),
        DraftDay(date="2026-10-02", morning="大熊猫基地（开园即入）", afternoon="宽窄巷子",
                 evening="锦里古街", spots=["大熊猫基地", "宽窄巷子", "锦里古街"]),
        DraftDay(date="2026-10-03", morning="武侯祠", afternoon="人民公园鹤鸣茶社",
                 evening="返程", spots=["武侯祠", "人民公园"]),
    ], budget_total=4981.0, warnings=["预算占用 99.6%（>90% 预警）"])
    from tripmate.models import ImageItem
    bb.profile.images = [ImageItem(spot=s, path=generate_placeholder(s), source="本地示意配图（模拟数据模式，非实景）")
                         for s in ("大熊猫基地", "宽窄巷子", "锦里古街", "武侯祠", "人民公园", "春熙路太古里")]

    bus = StatusBus()
    runner = TeamRunner(bb, bus)
    ctx = TeamContext(bb=bb, bus=bus, state=TeamState(), jobs=runner._jobs,
                      runner=runner, run_id="finalsmoke001")

    events = []

    async def watch():
        q = bus.subscribe()
        while True:
            ev = await q.get()
            events.append(ev)
            if ev["kind"] in ("STATUS_COMPLETED", "STATUS_ERROR"):
                return

    w = asyncio.create_task(watch())
    await asyncio.sleep(0)  # 让 watch 先完成 subscribe（_deliver_final 无挂起点，事件会瞬时投递）
    result = await _deliver_final(ctx)
    await w

    print("=== deliver_final 返回 ===")
    import json as _json
    print(_json.dumps({k: v for k, v in _json.loads(result).items() if k != "orders"},
                      ensure_ascii=False, indent=1))
    final = bb.profile.final
    print("\n=== 验收检查 ===")
    checks = {
        "final 分区已写入": final is not None,
        "PDF 文件存在": os.path.exists(final.pdf_path),
        "PDF > 10KB": os.path.getsize(final.pdf_path) > 10000,
        "订单含车票+酒店": {o["type"] for o in final.order_summary} == {"车票", "酒店"},
        "订单附直达链接": all(o["link"] for o in final.order_summary),
        "总价 = 609×2人×2程 + 488×2晚": abs(final.total_price - (609 * 2 * 2 + 488 * 2)) < 0.01,
        "STATUS_COMPLETED 已推送": any(e["kind"] == "STATUS_COMPLETED" for e in events),
    }
    for k, v in checks.items():
        print(("✓" if v else "✗"), k)
    print("\n通过 %d/%d" % (sum(checks.values()), len(checks)))
    print("PDF 路径:", final.pdf_path)


if __name__ == "__main__":
    asyncio.run(main())
