"""端到端冒烟②：collect 阶段四 Agent 对等协同 → 行程草稿（真实 LLM + 降级外部数据）。"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate.planning import compute_budget  # noqa: E402
from tripmate.session import Session  # noqa: E402

USER_INPUT = ("帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，"
              "预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，"
              "必去大熊猫基地。")


async def wait_draft(s: Session, timeout_s: int = 480) -> bool:
    """等待阶段完成事件（草稿就绪 = 阶段循环 + 护栏均已结束，黑板分区齐备）。"""
    while True:
        try:
            kind, data = await asyncio.wait_for(s.team_events.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print("  [watch] 超时")
            return False
        if kind == "draft_ready":
            return True
        if kind == "error":
            print("  [watch] 团队错误:", data)
            return False


async def main() -> None:
    s = Session()
    print("=== ① 用户输入 → Chatter 抽取并启动团队 ===")
    reply = await s.handle_user_message(USER_INPUT)
    print("Chatter:", reply[:300])
    print("\n=== ② 团队后台运行（状态事件流节选见 logs/tripmate.log） ===")
    ok = await wait_draft(s)
    assert ok, "草稿未生成"

    print("\n=== ③ 检查黑板各分区 ===")
    p = s.bb.profile
    print("guide_digest 来源数:", len(p.guide_digest))
    for g in p.guide_digest:
        print("   -", g.source_name, "| spots:", g.spots[:4], "| ref:", g.reference_only)
    print("tickets:", len(p.tickets), "| 已勾选:", next((t.train_no for t in p.tickets if t.selected), None))
    for t in p.tickets[:5]:
        print(f"   - {t.train_no} {t.depart_time}→{t.arrive_time} ¥{t.price} score={t.score} sel={t.selected} ref={t.reference_only}")
    print("hotels:", len(p.hotels), "| 已勾选:", next((h.name for h in p.hotels if h.selected), None))
    for h in p.hotels[:5]:
        print(f"   - {h.name} ¥{h.price_per_night}/晚 {h.distance_km}km 评分{h.rating} score={h.score} sel={h.selected} ref={h.reference_only}")
    print("weather:", json.dumps(p.weather.get("days", []), ensure_ascii=False)[:200])
    print("plan_input conflicts:", (p.plan_input.conflicts if p.plan_input else None))
    if p.draft:
        print("\n=== ④ 草稿 ===")
        for i, d in enumerate(p.draft.days):
            print(f"D{i + 1} {d.date} 上午:{d.morning[:30]} | 下午:{d.afternoon[:30]} | 晚上:{d.evening[:25]} | {d.spots}")
        b = compute_budget(p, p.draft)
        print("预算合计:", b["total"], "｜占用:", f"{b['occupancy']:.0%}" if b["occupancy"] else "-", "｜预警:", b["warnings"])
    else:
        print("!! 草稿未生成")


if __name__ == "__main__":
    asyncio.run(main())
