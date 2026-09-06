"""HTML 路书渲染包（主渲染路径）：TravelProfile → 唐风夜色 HTML → Chromium 打印 PDF。

入口 render(profile, run_id) 由 pdf_gen.build_pdf 调用（经 asyncio.to_thread 进工作线程）。
渲染异常向上抛，由 pdf_gen 统一降级 reportlab cartoon；依赖（playwright/pymupdf）在
engine 内懒加载，本包被 import 不要求它们已安装。版式风格复刻 learning/陕西3天2晚
懒人版旅行路书_图文版.pdf（决策记录见 docs/pdf-template-plan.md 2026-09-06）。
"""

from .engine import render

__all__ = ["render"]
