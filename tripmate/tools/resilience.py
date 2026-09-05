"""外部调用统一容错（§2.3）：单次超时 30s，重试 2 次（指数退避+抖动），仍失败抛 ServiceUnavailable。

另提供 cancel_with_grace：被 MCP/anyio 清理阶段挂住的任务无法靠 wait_for 超时止损
（2026-09-04 事故：wait_for 等任务真正退出，而清理自己挂住 → 上层超时永不生效），
统一走"取消 → 短宽限 → 抛弃"路径，抛弃任务登记引用直至其真正结束。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")

logger = logging.getLogger("tripmate.tools.resilience")

# 被抛弃的挂死任务登记表：持有强引用防 GC，done 回调自动出册并消费异常
_ABANDONED: set[asyncio.Task] = set()
# 登记量告警阈值：MCP stdio 被抛弃任务的清理（mcp 2.1.1 自带"关 stdin→等待→Job Object
# 硬杀进程树"且在 cancellation shield 内）通常仍会走完，仅清理协程本身永久卡死才真泄漏；
# 超阈值说明出现系统性挂死，需人工介入
_ABANDONED_WARN = 8


class ServiceUnavailable(RuntimeError):
    """外部服务不可用：调用方应走降级路径并明确提示。"""


def _backoff_delay(base_s: float, attempt: int) -> float:
    """重试延迟：指数退避（base × 2^attempt，上限 30s）+ ±50% 抖动。

    固定间隔会让并发查询（如攻略 7 路）在限流窗口上齐步重试，放大突发。"""
    return min(base_s * (2 ** attempt), 30.0) * random.uniform(0.5, 1.5)


def _consume_task_result(task: asyncio.Task) -> None:
    """done 回调：显式取回任务结果/异常，防事件循环刷 'exception was never retrieved'。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("被抛弃的挂死任务最终以 %s: %s 结束", type(exc).__name__, exc)


def _register_abandoned(task: asyncio.Task) -> None:
    _ABANDONED.add(task)
    if len(_ABANDONED) >= _ABANDONED_WARN:
        logger.warning("被抛弃的挂死任务已达 %d 个（疑似外部通道系统性挂死，请检查 MCP/网络）", len(_ABANDONED))
    task.add_done_callback(_ABANDONED.discard)
    task.add_done_callback(_consume_task_result)


async def cancel_with_grace(task: asyncio.Task, grace_s: float = 5.0) -> bool:
    """取消任务并最多等 grace_s 让其自行清理；仍未退出则抛弃（返回 True）。

    抛弃 = 不再等待其清理完成（挂死的 aclose 可能永远不结束），任务留登记表直至真正结束。
    若等待宽限期间本协程又被取消（如用户停止）：登记后透传取消。

    grace_s 默认 0.5→5s（2026-09-05）：MCP stdio 的清理序列（关 stdin → 等待退出 →
    Job Object 硬杀进程树）本身需要数秒，0.5s 几乎必然把"正在正常收尾"误判为挂死；
    抛弃路径不阻塞调用方主流程，多等几秒只影响罕见的超时分支。"""
    task.cancel()
    try:
        _, pending = await asyncio.wait({task}, timeout=grace_s)
    except asyncio.CancelledError:
        _register_abandoned(task)
        raise
    if not pending:
        if not task.cancelled():
            task.exception()  # 取回异常防警告
        return False
    _register_abandoned(task)
    logger.warning("任务 %s 取消后 %ss 内未退出（清理疑似挂死），已抛弃", task.get_name(), grace_s)
    return True


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
                await asyncio.sleep(_backoff_delay(delay_s, attempt))
        except Exception as e:  # noqa: BLE001 — 统一容错边界
            last_err = e
            if attempt < retries:
                await asyncio.sleep(_backoff_delay(delay_s, attempt))
    raise ServiceUnavailable(f"{what}连续 {retries + 1} 次失败：{last_err}")
