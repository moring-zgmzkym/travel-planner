"""PDF 生成入口（§4.5）：主路径 HTML→Chromium 路书（themes 主题注册表），降级 reportlab cartoon。

- 主渲染器为 `pdf_html`（Jinja2 + Playwright）；主题由 template 参数指定
  （lushu=出片之旅红金杂志 / elegant=典雅墨卷 / executive=行政公文，见 pdf_html/themes.py）。
- 降级触发：HTML 渲染任何异常（依赖缺失/浏览器不可用/排版失败），或 .env 设
  `PDF_RENDERER=reportlab`（一键回退旧行为的应急开关，恒用 cartoon，不随主题）。
- template 为 None/未知值（含旧会话残留的 "cartoon"）时回退默认主题，绝不因此切到 reportlab。
LLM 只提供内容，不参与排版，保证输出确定性。
"""

from __future__ import annotations

import time

from .config import PDF_RENDERER
from .models import TravelProfile
from .status import AUDIT


def render_reportlab(profile: TravelProfile, run_id: str, template: str | None = None) -> str:
    """reportlab 直渲染（降级引擎与脚本验证用）。未知名一概回退默认模板，
    兼容存量会话 basic_info.template 持久化的旧模板名。"""
    from .pdf_templates import get_template
    try:
        tpl = get_template(template)
    except ValueError:
        tpl = get_template(None)
    return tpl.render(profile, run_id)


def build_pdf(profile: TravelProfile, run_id: str, template: str | None = None,
              on_fallback=None) -> str:
    """渲染最终行程 PDF，返回成品绝对路径。

    template：HTML 主题名（None/未知回退默认主题 "lushu"）。
    on_fallback(reason)：HTML 降级 reportlab 时的可选回调（在 to_thread 工作线程内被
    调用；team.py 用 run_coroutine_threadsafe 转时间线提示，回调自身异常不影响交付）。
    """
    if (PDF_RENDERER or "html") != "html":
        return render_reportlab(profile, run_id, None)
    from .pdf_html import render as render_html
    from .pdf_html.themes import resolve_theme
    theme_name = resolve_theme(template)
    t0 = time.monotonic()
    try:
        return render_html(profile, run_id, theme=theme_name)
    except Exception as exc:  # noqa: BLE001 — 渲染失败降级，不阻塞定稿交付
        AUDIT.observation("PdfGen", f"HTML 渲染失败[{theme_name}]（{time.monotonic() - t0:.1f}s，"
                                    f"{type(exc).__name__}: {exc}），降级 reportlab cartoon")
        if on_fallback is not None:
            try:
                on_fallback(f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 — 通知失败不影响出 PDF
                pass
        return render_reportlab(profile, run_id, None)
