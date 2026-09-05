"""端到端冒烟③：草稿反馈修订 → 确认 → 配图 + PDF 定稿全流程（企划书 §5.1/§4.5）。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate.session import Session  # noqa: E402

USER_INPUT = ("帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，"
              "预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，"
              "必去大熊猫基地。")


async def wait_event_liveness(s: Session, target: set[str], *,
                              total_timeout_s: int = 3000, idle_timeout_s: int = 180,
                              label: str = "事件"):
    """活性感知的团队事件等待（2026-09-05 修复）：

    此前 wait_draft 固定 1500s——主通道故障夜（每轮先探测主模型 150s 超时再走次级）
    检查点重跑会吃满预算误报"草稿未生成"。现改为双通道等待：
    - 总上限 3000s（硬顶）；
    - 总线静默 180s（无任何心跳/状态事件）才判定真挂死快速失败；
    - 团队事件命中 target → 成功返回；error → 终止。
    """
    loop = asyncio.get_running_loop()
    sub = s.bus.subscribe()
    start = last_activity = loop.time()
    try:
        while True:
            now = loop.time()
            if now - start > total_timeout_s:
                print(f"  [watch] {label} 总超时（{total_timeout_s}s）")
                return None
            idle_left = idle_timeout_s - (now - last_activity)
            total_left = total_timeout_s - (now - start)
            team_ev = asyncio.create_task(s.team_events.get())
            bus_ev = asyncio.create_task(sub.get())
            done, pending = await asyncio.wait({team_ev, bus_ev},
                                               timeout=max(0.0, min(idle_left, total_left)),
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if not done:
                print(f"  [watch] {label} 总线静默 {idle_timeout_s}s，判定真挂死")
                return None
            for t in done:
                if t is team_ev:
                    kind, data = item = t.result()
                    if kind in target:
                        return kind, data
                    if kind == "error":
                        print("  [watch] 团队错误:", data)
                        return None
                else:
                    last_activity = loop.time()  # 心跳/状态事件 = 活着，继续等
    finally:
        s.bus.unsubscribe(sub)


async def wait_draft(s: Session, timeout_s: int = 1500) -> bool:
    """等待草稿就绪（活性感知：主备切换与检查点增量重跑不再误判超时）。"""
    got = await wait_event_liveness(s, {"draft_ready"}, label="草稿")
    return got is not None


async def wait_final(s: Session, timeout_s: int = 1500) -> bool:
    got = await wait_event_liveness(s, {"completed"}, label="定稿")
    return got is not None


async def main() -> None:
    s = Session()
    print("=== ① 输入用例 → 等待草稿（等待 3 秒后中途改预算，验证检查点+增量重跑）===")
    await s.handle_user_message(USER_INPUT)
    await asyncio.sleep(3)
    print("\n>>> 中途修改：预算改成 5000（团队后台运行中，§5.3）")
    reply = await s.handle_user_message("预算改成 5000")
    print("Chatter:", reply[:150])
    ok = await wait_draft(s)
    assert ok, "草稿未生成"
    d0 = s.bb.profile.draft
    print(f"初版草稿 D2 下午：{d0.days[1].afternoon[:40] if len(d0.days) > 1 else '-'}")
    checkpoint_events = [e for e in s.bus.history() if e.get("kind") == "STATUS_CHECKPOINT"]
    print("检查点事件:", [e["text"][:80] for e in checkpoint_events][:3])

    print("\n=== ② 提交反馈：'第 2 天换成都江堰'（§4.5 草稿循环）===")
    reply = await s.handle_user_message("第 2 天换成都江堰")
    print("Chatter:", reply[:200])
    ok = await wait_draft(s)
    assert ok, "修订草稿未生成"
    d1 = s.bb.profile.draft
    all_text = "".join(d.afternoon + d.morning + d.evening + " ".join(d.spots) for d in d1.days)
    has_dujiangyan = "都江堰" in all_text
    print(f"修订后 D2 下午：{d1.days[1].afternoon[:40] if len(d1.days) > 1 else '-'}")
    print("包含都江堰:", has_dujiangyan)

    print("\n=== ③ 确认草稿 → 定稿（配图 + PDF）===")
    reply = await s.handle_user_message("确认，就这样")
    print("Chatter:", reply[:200])
    ok = await wait_final(s)
    final = s.bb.profile.final
    print("\n=== ④ 检查成果 ===")
    print("final 就绪:", bool(final))
    if final:
        import os
        print("PDF 存在:", os.path.exists(final.pdf_path), "｜大小:", os.path.getsize(final.pdf_path), "B")
        print("PDF 路径:", final.pdf_path)
        print("订单项数:", len(final.order_summary), "｜合计:", final.total_price)
        from tripmate.llm import usage_summary
        u = usage_summary()
        print(f"token 消耗: {u['total_tokens']} / {u['limit']}（验收 #14）")
    print("图片数:", len(s.bb.profile.images))
    print("修订轮数:", (s.bb.profile.draft_feedback.rounds_used if s.bb.profile.draft_feedback else 0))


if __name__ == "__main__":
    asyncio.run(main())
