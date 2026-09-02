"""阶段 0 渲染器 Spike（方向二决策门，docs/html-designer-plan.md §四 阶段 0）。

验证三件事并输出决策记录：
0.1 Playwright Chromium：中文字体 / 本地图片 file:///（含中文与空格路径）/ inline SVG /
    @page 边距 / break-inside 分页控制 → A4 PDF
0.2 Edge CLI 兜底：独立 --user-data-dir、--no-pdf-header-footer、非 ASCII 输出路径、
    失败静默检测（事后校验 %PDF 头/页数）
0.3 @page margin 与 page.pdf(margin) 优先级行为

运行：python scripts/designer_poc.py
产物：outputs/designer_poc/*.pdf 与决策摘要（stdout）。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

OUT_DIR = BASE_DIR / "outputs" / "designer_poc"
IMAGE_DIR = BASE_DIR / "outputs" / "images"


def _sample_image() -> str:
    """取一张现有示意图（无则生成占位图），返回 Path.as_uri() 形式。"""
    if not IMAGE_DIR.exists() or not any(IMAGE_DIR.iterdir()):
        from tripmate.tools.imagegen import generate_placeholder
        p = Path(generate_placeholder("测试景点"))
    else:
        p = next(x for x in IMAGE_DIR.iterdir() if x.suffix.lower() in (".jpg", ".png"))
    return p.resolve().as_uri()


def _poc_html(img_uri: str) -> str:
    """覆盖全部决策门关注点的单页文档：@page 边距、中文字体、file:// 图片、SVG、分页。"""
    cards = "".join(
        f'<div class="card"><h3>卡片 {i}</h3><p>break-inside: avoid 分页保护验证'
        f'——本卡片不应被拦腰截断。</p></div>' for i in range(1, 13)
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>POC</title><style>
@page {{ size: A4; margin: 18mm 15mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Microsoft YaHei", "DengXian", "SimSun", sans-serif;
       color: #23232a; margin: 0; }}
.hero {{ background: linear-gradient(135deg, #2f6fed, #6c4de0); color: #fff;
        border-radius: 14px; padding: 28px; }}
.hero h1 {{ margin: 0 0 6px; font-size: 26px; }}
.hero img {{ width: 100%; height: 180px; object-fit: cover; border-radius: 10px; margin-top: 14px; }}
.chart-wrap {{ background: #f6f8fc; border-radius: 12px; padding: 16px; margin: 14px 0; }}
.card {{ background: #fff; border: 1px solid #e3e6ee; border-radius: 10px;
        padding: 12px 16px; margin-bottom: 10px; break-inside: avoid; }}
.card h3 {{ margin: 0 0 4px; font-size: 14px; }}
.card p {{ margin: 0; font-size: 12px; color: #5a5f6b; }}
</style></head><body>
<div class="hero"><h1>成都三日 · 行程规划书</h1>
<p>2026-10-01 → 2026-10-03 · 高铁往返 · 2 人 · 中等节奏</p>
<img src="{img_uri}" alt="示意配图"></div>
<div class="chart-wrap"><h2 style="margin:4px 0 10px;font-size:16px">预算构成（inline SVG 环图）</h2>
<svg width="420" height="180" viewBox="0 0 420 180">
  <circle cx="90" cy="90" r="60" fill="none" stroke="#e3e6ee" stroke-width="26"/>
  <circle cx="90" cy="90" r="60" fill="none" stroke="#2f6fed" stroke-width="26"
          stroke-dasharray="188 377" transform="rotate(-90 90 90)"/>
  <text x="90" y="86" text-anchor="middle" font-size="18" font-weight="bold" fill="#23232a">¥4981</text>
  <text x="90" y="108" text-anchor="middle" font-size="11" fill="#8a8f9b">预算合计</text>
  <g font-size="12" fill="#3a3f4b">
    <rect x="200" y="46" width="14" height="14" rx="3" fill="#2f6fed"/><text x="222" y="58">交通 ¥1218 × 2 人</text>
    <rect x="200" y="76" width="14" height="14" rx="3" fill="#6c4de0"/><text x="222" y="88">住宿 ¥976（2 晚）</text>
    <rect x="200" y="106" width="14" height="14" rx="3" fill="#22a06b"/><text x="222" y="118">门票+餐饮+备用金</text>
  </g>
</svg></div>
{cards}
<!--TRIPMATE-END-->
</body></html>"""


def _pdf_pages(path: Path) -> int:
    import fitz
    with fitz.open(path) as doc:
        return doc.page_count


def _discover_chromium() -> str | None:
    """版本漂移兜底：ms-playwright 下任意 chromium-* 内核（含 headless shell）。"""
    import os
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not root.exists():
        return None
    for pat in ("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
                "chromium-*/chrome-win64/chrome.exe",
                "chromium-*/chrome-win/chrome.exe"):
        hits = sorted(root.glob(pat))
        if hits:
            return str(hits[-1])
    return None


def poc_playwright() -> None:
    from playwright.sync_api import sync_playwright

    html = _poc_html(_sample_image())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = OUT_DIR / "poc.html"
    html_path.write_text(html, encoding="utf-8")

    exe = _discover_chromium()
    print(f"[0.1] 默认内核不可用（1.58 期望 1208，本机 1234），executable_path={exe}")
    # 0.3 优先级实验：同一份 @page margin:18mm 的 HTML，page.pdf 分别传 margin=0 与 margin=30mm
    # Chromium 行为：@page margin 优先于 pdf() options margin（以产物页数/内容位置判断）
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=30_000)
        page.pdf(path=str(OUT_DIR / "poc_pw_page_margin0.pdf"), margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        page.pdf(path=str(OUT_DIR / "poc_pw_page_margin30.pdf"),
                 margin={"top": "30mm", "bottom": "30mm", "left": "30mm", "right": "30mm"})
        browser.close()
    for name in ("poc_pw_page_margin0.pdf", "poc_pw_page_margin30.pdf"):
        p = OUT_DIR / name
        head = p.read_bytes()[:5] if p.exists() else ""
        print(f"[0.1/0.3] Playwright {name}: exists={p.exists()} head={head!r} "
              f"pages={_pdf_pages(p) if p.exists() else '-'} size={p.stat().st_size if p.exists() else 0}")
    print(f"[0.1] console errors: {errors or '无'}")


def poc_edge() -> None:
    import os
    exe_candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    exe = next((p for p in exe_candidates if p.exists()), None)
    if not exe:
        print("[0.2] Edge 未找到，兜底通道不可用")
        return
    html_path = OUT_DIR / "poc.html"
    out = OUT_DIR / "poc_edge_中文输出路径.pdf"
    if out.exists():
        out.unlink()
    with tempfile.TemporaryDirectory() as ud:
        cmd = [str(exe),
               "--headless",
               f"--user-data-dir={ud}",
               "--no-pdf-header-footer",
               "--disable-gpu",
               "--no-first-run",
               f"--print-to-pdf={out}",
               html_path.resolve().as_uri()]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    ok = out.exists() and out.read_bytes()[:5] == b"%PDF-" and out.stat().st_size > 1000
    print(f"[0.2] Edge CLI: rc={r.returncode} exists={out.exists()} head_ok={ok} "
          f"pages={_pdf_pages(out) if ok else '-'} size={out.stat().st_size if out.exists() else 0}")
    if not ok:
        print(f"[0.2] stderr tail: {(r.stderr or '')[-300:]}")


if __name__ == "__main__":
    poc_playwright()
    poc_edge()
    print("Spike 完成，产物见 outputs/designer_poc/")
