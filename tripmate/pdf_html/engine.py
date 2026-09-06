"""HTML→PDF 引擎：Playwright 无头 Chromium 三段渲染（封面/正文/封底）+ pymupdf 合并后处理。

安全与稳定设计（决策记录见 docs/pdf-template-plan.md 2026-09-06）：
- playwright / pymupdf 懒加载——本模块被 import 不代表依赖已安装，缺失由 pdf_gen 降级；
- sync API 只能在 asyncio.to_thread 工作线程内调用（事件循环线程内会抛错，绝不内联）；
- 模块级锁串行化渲染，防并发多开浏览器；
- launch / set_content / pdf 三段显式超时，任何异常向上抛、由 pdf_gen 统一降级；
- 图片/二维码全部 data URI / 内联 SVG，set_content 渲染：零临时文件、零外部请求；
- 封面/封底 margin:0 全出血、正文 @page 底部留 16mm 页码带（页码合并后盖章，不压正文）；
- 单页段若因 mm 取整溢出为 2 页，取第 1 页兜底并记审计。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..models import TravelProfile
from ..status import AUDIT
from .context import build_context, output_path

_LOCK = threading.Lock()
_TPL_DIR = Path(__file__).parent / "templates"

# 章节标题 → PDF 书签（在正文中定位，找不到的条目跳过）
_TOC_TITLES = ("行程总览", "天气与穿搭", "推荐订单清单", "逐日行程", "预算核算",
               "实景速览", "推荐酒店", "美食推荐", "行前准备", "应急预案")


def _env():
    from jinja2 import Environment, FileSystemLoader
    return Environment(loader=FileSystemLoader(str(_TPL_DIR)), autoescape=True)


def _launch_chromium(pw):
    """优先捆绑 Chromium，缺浏览器二进制（版本失配/未下载）时改用系统 Edge。"""
    try:
        return pw.chromium.launch(headless=True, timeout=60_000)
    except Exception:  # noqa: BLE001 — 换 Edge 通道重试一次
        return pw.chromium.launch(headless=True, channel="msedge", timeout=60_000)


def _render_part(page, html: str, single_page: bool) -> bytes:
    page.set_content(html, wait_until="load", timeout=30_000)
    pdf = page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                   margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    if single_page:
        import pymupdf
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        try:
            if doc.page_count > 1:
                AUDIT.observation("PdfHtml", f"单页段渲染出 {doc.page_count} 页（mm 取整溢出），取第 1 页兜底")
                doc.select([0])
                pdf = doc.tobytes(garbage=3, deflate=True)
        finally:
            doc.close()
    return pdf


def _render_all(parts: list[tuple[str, bool]]) -> list[bytes]:
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = _launch_chromium(pw)
        try:
            page = browser.new_page()
            return [_render_part(page, html, single) for html, single in parts]
        finally:
            browser.close()
    finally:
        pw.stop()


def _assemble(pdfs: list[bytes], out: Path, ctx: dict) -> int:
    """合并三段 → 盖页码 → 加书签 → 元数据 → 同目录临时文件原子落盘。返回总页数。"""
    import pymupdf
    merged = pymupdf.open()
    try:
        for part in pdfs:
            with pymupdf.open(stream=part, filetype="pdf") as src:
                merged.insert_pdf(src)
        total = merged.page_count
        gray = (124 / 255, 122 / 255, 140 / 255)          # 正文页码 #7C7A8C
        light = (138 / 255, 148 / 255, 176 / 255)         # 封底页码 #8A94B0
        for i in range(1, total):                          # 封面不盖页码
            page = merged[i]
            text = f"{i + 1} / {total}"
            w = pymupdf.get_text_length(text, fontname="helv", fontsize=8.5)
            page.insert_text(((page.rect.width - w) / 2, page.rect.height - 22.7),  # 8mm 处（页码带内）
                             text, fontname="helv", fontsize=8.5,
                             color=light if i == total - 1 else gray)
        toc = [[1, "封面", 1]]
        for title in _TOC_TITLES:
            for pno in range(1, total - 1):
                if merged[pno].search_for(title):
                    toc.append([1, title, pno + 1])
                    break
        toc.append([1, "封底", total])
        merged.set_toc(toc)
        b = ctx["basic"]
        merged.set_metadata({
            "title": f"TripMate 旅行路书 · {b['destination']}",
            "author": "TripMate 多 Agent 系统",
            "subject": f"{b['origin']}—{b['destination']} {b['dates_txt']} 行程计划",
            "creator": "TripMate",
            "producer": "TripMate pdf_html (Chromium)",
        })
        tmp = out.with_name(out.stem + ".tmp.pdf")
        merged.save(str(tmp), garbage=3, deflate=True)
        tmp.replace(out)                                   # 同目录替换，避免跨卷
        return total
    finally:
        merged.close()


def render(profile: TravelProfile, run_id: str) -> str:
    """渲染唐风路书 PDF，返回成品绝对路径（与 reportlab 路径同一命名规则）。"""
    t0 = time.monotonic()
    out = output_path(profile, run_id)
    ctx = build_context(profile)
    env = _env()
    css = (_TPL_DIR / "lushu.css").read_text(encoding="utf-8")
    parts = [
        (env.get_template("cover.html.j2").render(css=css, **ctx), True),
        (env.get_template("body.html.j2").render(css=css, **ctx), False),
        (env.get_template("back.html.j2").render(css=css, **ctx), True),
    ]
    with _LOCK:  # 串行化：防并发会话同时多开 Chromium
        pdfs = _render_all(parts)
        total = _assemble(pdfs, out, ctx)
    AUDIT.observation("PdfHtml", f"HTML 路书渲染完成：{out.name}（{total} 页，"
                                 f"{time.monotonic() - t0:.1f}s，Chromium）")
    return str(out)
