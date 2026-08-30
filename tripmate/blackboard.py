"""共享黑板（企划书 §3.6）：版本号递增、changelog 追加、写入串行化（风险 #6）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from .models import ChangelogEntry, TravelProfile, WriterName


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Blackboard:
    """所有 Agent 交换状态的唯一载体。写入经 asyncio.Lock 串行执行，版本号 +1。"""

    def __init__(self) -> None:
        self._profile = TravelProfile()
        self._lock = asyncio.Lock()

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
            return self._profile.version

    async def apply_basic_info(self, updates: dict[str, Any], writer: WriterName, reason: str) -> int:
        """合并式更新 basic_info（逐字段记 changelog，供变更影响分析 §5.3）。"""
        async with self._lock:
            basic = self._profile.basic_info.model_copy()
            for key, new_val in updates.items():
                if not hasattr(basic, key):
                    continue
                old_val = getattr(basic, key)
                if old_val == new_val:
                    continue
                setattr(basic, key, new_val)
                self._profile.changelog.append(
                    ChangelogEntry(
                        time=_now(), version=self._profile.version + 1, writer=writer,
                        section="basic_info", field=key,
                        old=_short(old_val), new=_short(new_val), reason=reason,
                    )
                )
            self._profile.basic_info = basic
            self._profile.version += 1
            self._profile.updated_at = _now()
            return self._profile.version

    async def apply_detail_info(self, updates: dict[str, Any], writer: WriterName, reason: str) -> int:
        """合并式更新 detail_info（hotel 子对象同样逐字段记录）。"""
        async with self._lock:
            detail = self._profile.detail_info.model_copy(deep=True)
            changed = False
            for key, new_val in updates.items():
                if key == "hotel" and isinstance(new_val, dict):
                    for hk, hv in new_val.items():
                        if hasattr(detail.hotel, hk) and getattr(detail.hotel, hk) != hv:
                            self._profile.changelog.append(
                                ChangelogEntry(
                                    time=_now(), version=self._profile.version + 1, writer=writer,
                                    section="detail_info", field=f"hotel.{hk}",
                                    old=_short(getattr(detail.hotel, hk)), new=_short(hv), reason=reason,
                                )
                            )
                            setattr(detail.hotel, hk, hv)
                            changed = True
                elif hasattr(detail, key):
                    old_val = getattr(detail, key)
                    if old_val == new_val:
                        continue
                    setattr(detail, key, new_val)
                    self._profile.changelog.append(
                        ChangelogEntry(
                            time=_now(), version=self._profile.version + 1, writer=writer,
                            section="detail_info", field=key,
                            old=_short(old_val), new=_short(new_val), reason=reason,
                        )
                    )
                    changed = True
            if changed:
                self._profile.detail_info = detail
                self._profile.version += 1
                self._profile.updated_at = _now()
            return self._profile.version

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
