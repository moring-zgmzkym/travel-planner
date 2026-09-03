"""外部调用统一容错（§2.3）：单次超时 30s，重试 2 次（间隔 5s），仍失败抛 ServiceUnavailable。"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class ServiceUnavailable(RuntimeError):
    """外部服务不可用：调用方应走降级路径并明确提示。"""


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    timeout_s: float = 30.0,
    retries: int = 2,
    delay_s: float = 5.0,
    what: str = "外部服务",
) -> T:
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(fn(), timeout=timeout_s)
        except asyncio.CancelledError as e:
            # anyio 作用域在超时取消后的清理阶段会抛出 CancelledError（BaseException，
            # except Exception 接不住，会炸穿调用方）。Task.cancelling()>0 才是外部
            # 真实取消（须透传）；否则视为清理期伪取消，按失败重试。
            if asyncio.current_task() is not None and asyncio.current_task().cancelling() > 0:
                raise
            last_err = e
            if attempt < retries:
                await asyncio.sleep(delay_s)
        except Exception as e:  # noqa: BLE001 — 统一容错边界
            last_err = e
            if attempt < retries:
                await asyncio.sleep(delay_s)
    raise ServiceUnavailable(f"{what}连续 {retries + 1} 次失败：{last_err}")
