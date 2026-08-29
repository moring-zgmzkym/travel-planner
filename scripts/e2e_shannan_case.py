"""端到端用例（用户指定场景）：成都→陕西，9月1日-9月3日，3天2夜，休闲节奏，最大预算 3500，偏好陕南周边。

流程：输入 → 等草稿（攻略+车票+酒店） → 确认 → 等定稿（PDF）。
全程打印画像抽取结果与产物校验点，便于核对抽取是否正确、PDF 是否产出。
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate.session import Session  # noqa: E402

USER_INPUT = ("帮我规划成都到陕西的旅游，9月1日到9月3日，3天2夜，休闲节奏，"
              "最大预算3500，偏好陕南周边的行程。")


async def wait_event(s: Session, target: set[str], timeout_s: int):
    """等待指定团队事件；error 也视为终止信号。返回 (kind, data) 或 None（超时）。"""
    while True:
        try:
            kind, data = await asyncio.wait_for(s.team_events.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            print("  [watch] 等待超时")
            return None
        if kind in target:
            return kind, data
        if kind == "error":
            print("  [watch] 团队错误:", data)
            return None


def show_profile(s: Session, tag: str) -> None:
    b = s.bb.profile.basic_info
    dt = s.bb.profile.detail_info
    print(f"[{tag}] origin={b.origin!r} destination={b.destination!r} days={b.days} "
          f"dates={b.travel_dates} date_text={b.date_text!r}")
    print(f"[{tag}] budget={b.budget} budget_max={b.budget_max} party={b.party_size} "
          f"mode={b.travel_mode!r} style={b.style} pace={dt.pace!r}")
    print(f"[{tag}] must_visit={dt.must_visit} special_needs={dt.special_needs!r}")
    print(f"[{tag}] defaults_applied={b.defaults_applied}")


async def main() -> None:
    s = Session()
    print("=== ① 用户输入 ===")
    print(USER_INPUT)
    reply = await s.handle_user_message(USER_INPUT)
    print("Chatter:", (reply or "")[:200])
    show_profile(s, "输入后")

    print("\n=== ② 等待草稿（攻略/车票/酒店收集 + 行程编排）===")
    got = await wait_event(s, {"draft_ready"}, timeout_s=900)
    if not got:
        show_profile(s, "失败现场")
        sys.exit(1)
    d = s.bb.profile.draft
    print("草稿天数:", len(d.days))
    for day in d.days:
        print(f"  {day.date} | 上午 {day.morning[:36]} | 下午 {day.afternoon[:36]} | 晚上 {day.evening[:36]}")
        print(f"        spots: {day.spots}")
    print("预算合计:", d.budget_total, "｜预警:", d.warnings)
    for src in (s.bb.profile.guide_digest or []):
        print("  攻略:", src.source_name, "｜spots:", src.spots[:5], "｜参考值:", src.reference_only)

    print("\n=== ③ 确认草稿 → 定稿（配图 + PDF）===")
    if "--direct-confirm" in sys.argv:
        # 诊断用旁路：hy3-free 通道会把工具调用文本化（<tool_sep:...>）导致确认丢失，
        # 此处直接调用 runner 确认，用于单独验证 配图→PDF 下游流水线。
        print("[旁路] 直接调用 runner.submit_feedback(confirmed=True)，不经过 Chatter")
        receipt = s.runner.submit_feedback(feedback="", confirmed=True)
        print("回执:", receipt)
    else:
        reply = await s.handle_user_message("确认，就这样")
        print("Chatter:", (reply or "")[:150])
    got = await wait_event(s, {"completed"}, timeout_s=900)
    if not got:
        sys.exit(1)
    final = s.bb.profile.final
    print("\n=== ④ 成果校验 ===")
    print("final 就绪:", bool(final))
    if final:
        print("PDF 存在:", os.path.exists(final.pdf_path), "｜大小:", os.path.getsize(final.pdf_path), "B")
        print("PDF 路径:", final.pdf_path)
        print("订单项数:", len(final.order_summary), "｜合计:", final.total_price)
    print("图片数:", len(s.bb.profile.images))
    show_profile(s, "定稿后")
    from tripmate.llm import usage_summary
    u = usage_summary()
    print(f"token 消耗: {u['total_tokens']} / {u['limit']}")


if __name__ == "__main__":
    asyncio.run(main())
