"""outputs 清理 CLI（2026-09-12）：默认 dry-run 预览，--apply 才真删。

用法：
    python scripts/cleanup_outputs.py                 # 预览将删清单与可释放空间
    python scripts/cleanup_outputs.py --apply         # 执行删除
    python scripts/cleanup_outputs.py --apply --pdf-grace 7   # 自定无引用 PDF 宽限期（天）

规则详见 tripmate/maintenance.py 模块注释：每个会话当前定稿的 PDF 永久保留；
分享中的 PDF 永久保留；被引用图片永不删；crops/pdf_baseline/tmp/旧日志直接清。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tripmate import maintenance  # noqa: E402
from tripmate.config import OUTPUT_DIR  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="清理 outputs 中无引用的产物（默认 dry-run 预览，不删任何东西）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认只预览）")
    ap.add_argument("--pdf-grace", type=float, default=3.0,
                    help="无引用 PDF 的保留宽限（天，默认 3；会话当前定稿的 PDF 不受此限，永久保留）")
    ap.add_argument("--min-age", type=float, default=1.0,
                    help="无引用图片的保留时长（天，默认 1；被画像引用的图片不受此限，永不删）")
    args = ap.parse_args()
    pdf_grace = max(0.0, args.pdf_grace)   # 负数会使宽限失效、无引用新 PDF 立即可删——钳到 ≥0
    min_age = max(0.0, args.min_age)

    try:
        plan = maintenance.plan_cleanup(pdf_grace_days=pdf_grace, min_age_days=min_age)
    except maintenance.MaintenanceAborted as e:
        print(f"[中止] {e}")
        sys.exit(2)

    print(f"扫描目录：{OUTPUT_DIR}")
    if plan.no_sessions_guard:
        print("[守卫] 未发现任何会话文件：本轮跳过 PDF 清理（只清纯产物），防止误删")
    for label, group in (("被取代/测试 PDF", plan.superseded_pdfs),
                         ("无引用图片", plan.orphan_images),
                         ("crops 缓存", plan.crops),
                         ("pdf_baseline 预览", plan.baseline),
                         ("tmp 崩溃残留", plan.tmp_residue),
                         ("旧 e2e 日志", plan.old_logs)):
        if group:
            size = sum(p.stat().st_size for p in group if p.exists())
            print(f"\n[{label}] {len(group)} 个，{size / 1e6:.1f} MB")
            for p in group[:8]:
                print(f"  - {p.name}")
            if len(group) > 8:
                print(f"  ...等 {len(group)} 个")
    print(f"\n合计：{plan.total_files} 个文件，可释放 {plan.freed_bytes / 1e6:.1f} MB")

    if not args.apply:
        print("\n[dry-run] 未删除任何文件。确认无误后追加 --apply 执行删除。")
        return
    try:
        result = maintenance.apply(plan)
    except maintenance.MaintenanceAborted as e:
        print(f"[中止] {e}")
        sys.exit(2)
    print(f"\n[完成] 已删除 {len(result.deleted)} 个文件，释放 {result.freed_bytes / 1e6:.1f} MB")
    if result.failed:
        print(f"[跳过] {len(result.failed)} 个删除失败（文件占用等），下次清理再试：")
        for f in result.failed[:5]:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
