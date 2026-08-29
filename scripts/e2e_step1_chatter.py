"""端到端冒烟①：Chatter 信息抽取 + 启动判定（真实 LLM，验收用例 §10.1 输入）。"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate.session import Session  # noqa: E402

USER_INPUT = ("帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，"
              "预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，"
              "必去大熊猫基地。")


async def main() -> None:
    s = Session()
    print("=== 用户输入 ===")
    print(USER_INPUT)
    print("\n=== Chatter 处理中（真实 LLM）===")
    reply = await s.handle_user_message(USER_INPUT)
    print("\n=== Chatter 回复 ===")
    print(reply)

    b = s.bb.profile.basic_info
    d = s.bb.profile.detail_info
    print("\n=== 抽取结果 ===")
    print("basic_info:", json.dumps(b.model_dump(), ensure_ascii=False, indent=1))
    print("detail_info:", json.dumps(d.model_dump(), ensure_ascii=False, indent=1))
    print("\n=== 检查点 ===")
    checks = {
        "出发地=上海": b.origin == "上海",
        "目的地=成都": b.destination == "成都",
        "天数=3": b.days == 3,
        "方式=高铁": b.travel_mode == "高铁",
        "日期": b.travel_dates[:1] == ["2026-10-01"],
        "风格含休闲/美食": "休闲" in b.style,
        "预算=6000": b.budget == 6000,
        "最大预算=7000": b.budget_max == 7000,
        "人数=2": (d.party_size or b.party_size) == 2,
        "酒店位置含春熙路": "春熙" in (d.hotel.location_pref or ""),
        "酒店价格区间[300,500]": d.hotel.price_range == [300, 500],
        "必去含熊猫基地": any("熊猫" in m for m in d.must_visit),
        "团队已启动": s.runner.active or s.runner._task is not None,
    }
    for k, v in checks.items():
        print(("✓" if v else "✗"), k)
    print("\n通过 %d/%d" % (sum(checks.values()), len(checks)))


if __name__ == "__main__":
    asyncio.run(main())
