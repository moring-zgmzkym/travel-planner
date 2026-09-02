"""PDF 生成入口（§4.5）：按模板名从注册表分发渲染。

版式实现位于 `tripmate/pdf_templates/`：基类提供公共积木（字体、样式、表格、
图片处理、封面），每个模板 = 主题配色 + build_story() 版式。
LLM 只提供内容，不参与排版，保证输出确定性。
"""

from __future__ import annotations

from .models import TravelProfile
from .pdf_templates import get_template


def build_pdf(profile: TravelProfile, run_id: str, template: str | None = None) -> str:
    """渲染最终行程 PDF；template 为空时使用默认模板（classic）。"""
    return get_template(template).render(profile, run_id)
