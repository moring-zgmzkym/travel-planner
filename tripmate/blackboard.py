"""共享黑板（企划书 §3.6）：版本号递增、changelog 追加、写入串行化（风险 #6）。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .models import ChangelogEntry, TravelProfile, WriterName

logger = logging.getLogger("tripmate.blackboard")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def coerced_field(model: BaseModel, key: str, value: Any) -> Any:
    """按模型 schema 宽松校验/转型单字段，返回转型后的值；非法抛 ValidationError。

    此前 apply_* 直接 setattr（pydantic 默认 validate_assignment=False）：LLM 传
    {"days": "3天"} 会静默写入，延迟到下游纯逻辑处才炸 TypeError、整阶段报废。
    现在写入口逐字段过 schema：pydantic 宽松模式放行 "3"→3、"6000"→6000.0 等
    常规转型；带单位文本由 chatter 层预 coerce（blackboard 这里是最后防线）。"""
    probe = type(model).model_validate({key: value})
    return getattr(probe, key)


class Blackboard:
    """所有 Agent 交换状态的唯一载体。写入经 asyncio.Lock 串行执行，版本号 +1。"""

    def __init__(self) -> None:
        self._profile = TravelProfile()
        self._lock = asyncio.Lock()
        # 持久化钩子（2026-09-05）：Session 注册；每次写入后锁外调用，失败绝不影响黑板主流程
        self.on_change: Callable[[], None] | None = None

    def _notify_change(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as e:  # noqa: BLE001 — 持久化失败不阻塞规划
            logger.warning("黑板持久化钩子失败（%s: %s）", type(e).__name__, e)

    def restore_profile(self, profile: TravelProfile) -> None:
        """重启恢复：整对象替换画像（版本号保留；changelog 由持久化层置空）。

        仅恢复场景使用——不触发 on_change（恢复过程中不回写磁盘）。"""
        self._profile = profile

    # ---- 读（无锁，Pydantic 对象替换式写保证读侧一致性） ----
    @property
    def profile(self) -> TravelProfile:
        return self._profile

    def read(self, section: str) -> Any:
        return getattr(self._profile, section)

    def version(self) -> int:
        return self._profile.version

    def clear_sections(self, sections: dict[str, Any], writer: WriterName, reason: str) -> None:
        """新 run 边界的同步清空（team.start() 受理后调用）。

        此刻上一轮团队任务已确认结束、新一轮任务尚未创建，事件循环上无并发写者，
        因此不走 asyncio.Lock，直接落 setattr + 版本号 + changelog（语义与 write 一致）。
        """
        for section, value in sections.items():
            old = getattr(self._profile, section)
            setattr(self._profile, section, value)
            self._profile.version += 1
            self._profile.updated_at = _now()
            self._profile.changelog.append(ChangelogEntry(
                time=_now(),
                version=self._profile.version,
                writer=writer,
                section=section,
                field=section,
                old=_short(old),
                new=_short(value),
                reason=reason,
            ))
        self._notify_change()

    # ---- 写（串行化 + changelog + 版本号） ----
    async def write(
        self,
        section: str,
        value: Any,
        writer: WriterName,
        reason: str = "",
        field: str = "",
    ) -> int:
        async with self._lock:
            old = getattr(self._profile, section)
            # pydantic 字段整体替换（list/dict/model 均可）
            setattr(self._profile, section, value)
            self._profile.version += 1
            self._profile.updated_at = _now()
            self._profile.changelog.append(ChangelogEntry(
                time=_now(),
                version=self._profile.version,
                writer=writer,
                section=section,
                field=field or section,
                old=_short(old),
                new=_short(value),
                reason=reason,
            ))
            version = self._profile.version
        self._notify_change()  # 锁外触发：同步磁盘写不拉长写锁临界区
        return version

    async def apply_basic_info(self, updates: dict[str, Any], writer: WriterName, reason: str) -> int:
        """合并式更新 basic_info（逐字段记 changelog，供变更影响分析 §5.3）。

        两段式（2026-09-05）：先在 model_copy 上逐字段校验/转型收集可写字段，
        全部通过后再统一落分区 + changelog + 版本号——非法字段跳过并记日志，
        绝不让单字段类型错误造成"changelog 已追加而分区未写"的状态分裂。"""
        changed = False
        async with self._lock:
            basic = self._profile.basic_info.model_copy()
            accepted: list[tuple[str, Any, Any]] = []
            for key, new_val in updates.items():
                if not hasattr(basic, key):
                    logger.warning("apply_basic_info 忽略未知字段 %r（writer=%s）", key, writer)
                    continue
                try:
                    new_val = coerced_field(basic, key, new_val)
                except (ValidationError, TypeError, ValueError) as e:
                    logger.warning("apply_basic_info 拒绝字段 %r=%r（%s: %s）", key, new_val,
                                   type(e).__name__, str(e)[:120])
                    continue
                old_val = getattr(basic, key)
                if old_val == new_val:
                    continue
                accepted.append((key, old_val, new_val))
            for key, old_val, new_val in accepted:
                setattr(basic, key, new_val)
                self._profile.changelog.append(
                    ChangelogEntry(
                        time=_now(), version=self._profile.version + 1, writer=writer,
                        section="basic_info", field=key,
                        old=_short(old_val), new=_short(new_val), reason=reason,
                    )
                )
                changed = True
            if changed:
                self._profile.basic_info = basic
                self._profile.version += 1
                self._profile.updated_at = _now()
            version = self._profile.version
        if changed:
            self._notify_change()
        return version

    async def apply_detail_info(self, updates: dict[str, Any], writer: WriterName, reason: str) -> int:
        """合并式更新 detail_info（hotel 子对象同样逐字段记录；两段式校验同 apply_basic_info）。"""
        async with self._lock:
            detail = self._profile.detail_info.model_copy(deep=True)
            # (目标对象, setattr 属性名, changelog 字段名, 旧值, 新值)
            accepted: list[tuple[Any, str, str, Any, Any]] = []
            for key, new_val in updates.items():
                if key == "hotel" and isinstance(new_val, dict):
                    for hk, hv in new_val.items():
                        if not hasattr(detail.hotel, hk):
                            logger.warning("apply_detail_info 忽略未知 hotel 字段 %r（writer=%s）", hk, writer)
                            continue
                        try:
                            hv = coerced_field(detail.hotel, hk, hv)
                        except (ValidationError, TypeError, ValueError) as e:
                            logger.warning("apply_detail_info 拒绝 hotel.%s=%r（%s: %s）", hk, hv,
                                           type(e).__name__, str(e)[:120])
                            continue
                        old_val = getattr(detail.hotel, hk)
                        if old_val == hv:
                            continue
                        accepted.append((detail.hotel, hk, f"hotel.{hk}", old_val, hv))
                elif hasattr(detail, key):
                    try:
                        new_val = coerced_field(detail, key, new_val)
                    except (ValidationError, TypeError, ValueError) as e:
                        logger.warning("apply_detail_info 拒绝字段 %r=%r（%s: %s）", key, new_val,
                                       type(e).__name__, str(e)[:120])
                        continue
                    old_val = getattr(detail, key)
                    if old_val == new_val:
                        continue
                    accepted.append((detail, key, key, old_val, new_val))
                else:
                    logger.warning("apply_detail_info 忽略未知字段 %r（writer=%s）", key, writer)
            changed = False
            for target, attr, field_name, old_val, new_val in accepted:
                setattr(target, attr, new_val)
                self._profile.changelog.append(
                    ChangelogEntry(
                        time=_now(), version=self._profile.version + 1, writer=writer,
                        section="detail_info", field=field_name,
                        old=_short(old_val), new=_short(new_val), reason=reason,
                    )
                )
                changed = True
            if changed:
                self._profile.detail_info = detail
                self._profile.version += 1
                self._profile.updated_at = _now()
            version = self._profile.version
        if changed:
            self._notify_change()
        return version

    # ---- 变更影响分析输入（§5.3）：自某版本以来用户侧（chatter）写入的变更 ----
    def user_changes_since(self, version: int) -> list[ChangelogEntry]:
        return [
            e for e in self._profile.changelog
            if e.version > version and e.writer == "chatter"
            and e.section in ("basic_info", "detail_info")
        ]

    def section_version(self, section: str) -> int:
        """指定分区最后一次被写入的版本号（未被写过返回 0）。"""
        for e in reversed(self._profile.changelog):
            if e.section == section:
                return e.version
        return 0

    def compact_json(self) -> str:
        """供提示词注入的紧凑视图（裁剪 changelog，只保留最近 10 条）。"""
        data = self._profile.model_dump(mode="json")
        data["changelog"] = data["changelog"][-10:]
        return json.dumps(data, ensure_ascii=False)


def _short(v: Any, limit: int = 80) -> Any:
    """changelog 旧值/新值压缩，避免大对象膨胀。"""
    s = v if isinstance(v, (int, float, bool, type(None))) else str(v)
    if isinstance(s, str) and len(s) > limit:
        return s[: limit - 3] + "..."
    return s
