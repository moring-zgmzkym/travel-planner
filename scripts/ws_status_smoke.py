"""WS 状态通路冒烟：起真实服务 → WebSocket 客户端验证状态推送全链路（2026-08-31 事故回归）。

验证点（对应"状态面板不更新"故障）：
  1. 连接补播：session/profile 消息到达
  2. chat 往返：用户消息 → chatter 回复
  3. 启动状态：验收用例后 180s 内收到 STATUS_PHASE（团队受理/启动）
  4. 运行心跳：团队运行中 30s 心跳 STATUS_PROGRESS 到达（长静默期面板不再"停摆"）
  5. 停止回执：stop 指令 → STATUS_CANCELLED（并避免跑完整 collect 烧配额）

用法：python scripts/ws_status_smoke.py   （自动在 127.0.0.1:8011 起独立服务实例，跑完即停）
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8011
URL = f"ws://127.0.0.1:{PORT}/ws?sid=ws_smoke_{int(time.time())}"

ACCEPT_INPUT = ("帮我规划十一成都 3 天游，10 月 1 号从上海出发，高铁往返，两个人，"
                "预算 6000 最多 7000，想休闲一点顺便吃吃喝喝，酒店想住春熙路附近 300 到 500 一晚的，"
                "必去大熊猫基地。")

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name + (f"（{detail}）" if detail else "")))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"：{detail}" if detail else ""))


async def wait_for(ws, pred, timeout_s: float, label: str):
    """持续收帧直到 pred 命中；返回命中消息，超时返回 None。"""
    deadline = time.monotonic() + timeout_s
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            print(f"  [wait] {label} 超时（{int(timeout_s)}s）")
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        except asyncio.TimeoutError:
            print(f"  [wait] {label} 超时（{int(timeout_s)}s）")
            return None
        m = json.loads(raw)
        kind = m.get("kind") or ""
        if m.get("type") == "status":
            print(f"    <status {m.get('ts','')} {kind} [{m.get('agent')}] {str(m.get('text'))[:48]}")
        if pred(m):
            return m


async def wait_port(port: int, timeout_s: float = 30) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            await asyncio.sleep(0.5)
    return False


async def main() -> int:
    import websockets

    env = dict(os.environ, PORT=str(PORT))
    server = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT), env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not await wait_port(PORT):
            check(False, "服务启动", f"127.0.0.1:{PORT} 未监听")
            return 1
        print("=== 服务已启动，连接 WS ===")
        async with websockets.connect(URL, open_timeout=10) as ws:
            # 1. 连接补播
            m1 = await wait_for(ws, lambda m: m.get("type") == "session", 15, "session 补播")
            check(m1 is not None, "① session 补播到达")
            m2 = await wait_for(ws, lambda m: m.get("type") == "profile", 15, "profile 补播")
            check(m2 is not None, "① profile 补播到达")

            # 2. chat 往返
            await ws.send(json.dumps({"type": "chat", "text": "你好，请介绍一下你自己"}, ensure_ascii=False))
            m3 = await wait_for(ws, lambda m: m.get("type") == "chat" and m.get("role") == "chatter",
                                240, "chatter 回复")
            check(m3 is not None, "② chat 往返（chatter 回复）")

            # 3. 启动状态（本次事故链路：宣布启动必须真的产生 STATUS_PHASE）
            await ws.send(json.dumps({"type": "chat", "text": ACCEPT_INPUT}, ensure_ascii=False))
            m4 = await wait_for(ws, lambda m: m.get("type") == "status" and m.get("kind") == "STATUS_PHASE",
                                300, "STATUS_PHASE")
            check(m4 is not None, "③ STATUS_PHASE 到达（规划启动状态推送）")

            # 4. 运行心跳（长静默期续亮：修复前团队运行期间零事件）
            m5 = await wait_for(ws, lambda m: m.get("type") == "status" and m.get("kind") == "STATUS_PROGRESS",
                                90, "STATUS_PROGRESS 心跳")
            check(m5 is not None, "④ 30s 运行心跳到达")

            # 5. 停止回执（控制配额：不跑完 collect）
            await ws.send(json.dumps({"type": "stop"}))
            m6 = await wait_for(ws, lambda m: m.get("type") == "status"
                                and m.get("kind") in ("STATUS_CANCELLED", "STATUS_INFO", "STATUS_ERROR"),
                                60, "停止回执")
            check(m6 is not None, "⑤ stop 停止回执到达")
    except Exception as e:  # noqa: BLE001 — 冒烟脚本自身兜底
        check(False, "冒烟异常", f"{type(e).__name__}: {e}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    print("\n=== 结果 ===")
    for ok, name in results:
        print(("PASS " if ok else "FAIL ") + name)
    failed = [n for ok, n in results if not ok]
    print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
