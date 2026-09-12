"""outputs 目录自动清理（2026-09-12）：只删确定无引用的产物，安全第一。

保留（不看年龄）：
- 每个会话当前定稿的 PDF（profile.final.pdf_url / pdf_path 的 basename——"每次对话的最终版"）
- 分享直链引用的 PDF（data/shares.json 未撤销条目）
- 会话画像引用的全部图片（显式字段 + 深度扫描兜底，防未来字段遗漏）

删除：
- 无引用旧 PDF（默认宽限 3 天：被新定稿取代/历史测试产物，留反悔期）
- 无引用散图（默认 24h：防在途规划刚下载未入画像的竞态）
- images/crops/（md5 缓存，按需重建）、pdf_baseline/（视觉验收预览）、
  outputs/*.tmp.pdf 崩溃残留、根目录旧 e2e 日志

fail-closed：任一会话/shares 文件损坏 → 抛 MaintenanceAborted 中止清理（无法确认引用集绝不删）；
一个会话文件都没有 → 跳过 PDF 清理（防全新环境把引用集为空误判成"全部无引用"）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR, IMAGE_DIR, OUTPUT_DIR, SESSIONS_DIR
from .status import AUDIT

SHARES_PATH = DATA_DIR / "shares.json"   # 分享索引（gateway 以别名引用同一文件）

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


class MaintenanceAborted(Exception):
    """引用集无法确认（文件损坏等）——清理中止，绝不带病删除。"""


@dataclass
class CleanupPlan:
    superseded_pdfs: list[Path] = field(default_factory=list)
    orphan_images: list[Path] = field(default_factory=list)
    crops: list[Path] = field(default_factory=list)
    baseline: list[Path] = field(default_factory=list)
    tmp_residue: list[Path] = field(default_factory=list)
    old_logs: list[Path] = field(default_factory=list)
    no_sessions_guard: bool = False   # True=因无任何会话文件而跳过了 PDF 清理

    @property
    def total_files(self) -> int:
        return (len(self.superseded_pdfs) + len(self.orphan_images) + len(self.crops)
                + len(self.baseline) + len(self.tmp_residue) + len(self.old_logs))

    @property
    def freed_bytes(self) -> int:
        total = 0
        for group in (self.superseded_pdfs, self.orphan_images, self.crops,
                      self.baseline, self.tmp_residue, self.old_logs):
            for p in group:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total


@dataclass
class CleanupResult:
    deleted: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    freed_bytes: int = 0


def _basename(p) -> str:
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]


def _iter_session_files() -> list[Path]:
    """新旧两种会话布局：sessions/{sid}.json（旧扁平/无用户名）+ sessions/{用户名}/{sid}.json。"""
    return sorted(SESSIONS_DIR.glob("*.json")) + sorted(SESSIONS_DIR.glob("*/*.json"))


def _walk_collect_image_names(node, into: set[str]) -> None:
    """深度扫描兜底：画像 JSON 里任何以图片扩展名结尾的字符串都计入引用
    （显式字段之外的未知/未来字段也不会被误删）。"""
    if isinstance(node, str):
        if node.lower().endswith(_IMG_EXTS):
            into.add(_basename(node))
    elif isinstance(node, dict):
        for v in node.values():
            _walk_collect_image_names(v, into)
    elif isinstance(node, list):
        for v in node:
            _walk_collect_image_names(v, into)


def collect_referenced() -> tuple[set[str], set[str], int]:
    """扫描全部会话文件 → (被引用 PDF 名集合, 被引用图片名集合, 会话文件数)。

    任一文件损坏抛 MaintenanceAborted（fail-closed）。"""
    pdf_refs: set[str] = set()
    img_refs: set[str] = set()
    files = _iter_session_files()
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise MaintenanceAborted(f"会话文件损坏：{p.name}（{e}）——无法确认引用集，已中止清理") from e
        profile = data.get("profile") or {}
        if not isinstance(profile, dict):
            continue
        final = profile.get("final") or {}
        if isinstance(final, dict):
            for k in ("pdf_url", "pdf_path"):
                v = final.get(k)
                if v:
                    pdf_refs.add(_basename(v))
        for h in profile.get("hotels") or []:
            if isinstance(h, dict) and h.get("image_path"):
                img_refs.add(_basename(h["image_path"]))
        for it in profile.get("images") or []:
            if isinstance(it, dict) and it.get("image_path"):
                img_refs.add(_basename(it["image_path"]))
        for cv in profile.get("cover_images") or []:
            if cv:
                img_refs.add(_basename(cv))
        _walk_collect_image_names(profile, img_refs)  # 深度扫描兜底
    return pdf_refs, img_refs, len(files)


def _load_share_refs() -> set[str]:
    if not SHARES_PATH.exists():
        return set()
    try:
        data = json.loads(SHARES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MaintenanceAborted(f"分享索引损坏：{SHARES_PATH.name}（{e}）——无法确认分享引用，已中止清理") from e
    return {info.get("file") for info in data.values()
            if isinstance(info, dict) and info.get("file") and not info.get("revoked")}


def _age_days(p: Path, now: float) -> float:
    try:
        return max(0.0, (now - p.stat().st_mtime) / 86400)
    except OSError:
        return 0.0   # stat 不了 → 视为最新（保守：不删）


def plan_cleanup(pdf_grace_days: float = 3.0, min_age_days: float = 1.0) -> CleanupPlan:
    """产出待删清单（纯只读，不删任何东西）。引用文件永不入选。"""
    plan = CleanupPlan()
    now = time.time()
    pdf_refs, img_refs, session_n = collect_referenced()   # 可能抛 MaintenanceAborted
    pdf_refs |= _load_share_refs()

    # PDF：顶层 *.pdf，非引用且超过宽限期 → 删；一个会话文件都没有 → 跳过（防全新环境误判）
    # 注意 *.tmp.pdf 崩溃残留归入下方 tmp_residue，此处跳过防双重计数
    for p in sorted(OUTPUT_DIR.glob("*.pdf")):
        if p.name.endswith(".tmp.pdf"):
            continue
        if p.name in pdf_refs:
            continue
        if session_n == 0:
            plan.no_sessions_guard = True
            continue
        if _age_days(p, now) < pdf_grace_days:
            continue
        plan.superseded_pdfs.append(p)

    # 散图：images 顶层非引用且超过保护窗 → 删
    for p in sorted(IMAGE_DIR.glob("*")):
        if not p.is_file() or p.suffix.lower() not in _IMG_EXTS:
            continue
        if _basename(p) in img_refs:
            continue
        if _age_days(p, now) < min_age_days:
            continue
        plan.orphan_images.append(p)

    # 纯产物：缓存 / 预览 / 崩溃残留 / 旧日志
    crops_dir, baseline_dir = IMAGE_DIR / "crops", OUTPUT_DIR / "pdf_baseline"
    plan.crops = [p for p in crops_dir.rglob("*") if p.is_file()] if crops_dir.exists() else []
    plan.baseline = [p for p in baseline_dir.rglob("*") if p.is_file()] if baseline_dir.exists() else []
    plan.tmp_residue = sorted(OUTPUT_DIR.glob("*.tmp.pdf"))
    plan.old_logs = [p for p in sorted(OUTPUT_DIR.glob("*.log")) if p.is_file()]
    return plan


def apply(plan: CleanupPlan) -> CleanupResult:
    """执行删除：逐文件 try，单个失败（如文件被占用）只记日志跳过，下次再清。"""
    result = CleanupResult()
    for group in (plan.superseded_pdfs, plan.orphan_images, plan.crops,
                  plan.baseline, plan.tmp_residue, plan.old_logs):
        for p in group:
            try:
                size = p.stat().st_size
                p.unlink()
                result.deleted.append(p)
                result.freed_bytes += size
            except OSError as e:
                result.failed.append(f"{p.name}: {e}")
    if result.deleted:
        AUDIT.output("Maintenance",
                     f"清理完成：删除 {len(result.deleted)} 个文件，"
                     f"释放 {result.freed_bytes / 1e6:.1f} MB"
                     + (f"；{len(result.failed)} 个失败跳过" if result.failed else ""))
    return result
