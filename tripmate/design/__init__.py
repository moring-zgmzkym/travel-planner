"""设计系统资源包（方向二 Designer 链路）。

- print.css：印刷级设计系统（套壳代码内联注入，不经 LLM）
- fragments.py：9 类组件标准片段（注入 Designer 提示词的 few-shot 素材）
- golden_sample.html：人工打磨的金样 body 片段（成都样例数据，注入生成轮提示词）

本包不依赖任何第三方库，html/css 均为文本资源。
"""

from __future__ import annotations

from pathlib import Path

DESIGN_DIR = Path(__file__).resolve().parent

DESIGNER_PIPELINE_VERSION = "v1"  # print.css/片段库/金样/提示词任一变更时 +1（进缓存键）

THEMES = ("theme-azure", "theme-warm", "theme-fresh", "theme-mono")
DEFAULT_THEME = "theme-azure"

DESIGNER_TEMPLATE_META = {
    "name": "designer",
    "display_name": "✨ AI 设计师排版",
    "description": "由 AI 版面设计现场创作的独特路书（生成约需额外几分钟，失败自动回退经典模板）",
    "scenes": "想要独一无二的设计",
}


def load_print_css() -> str:
    return (DESIGN_DIR / "print.css").read_text(encoding="utf-8")


def load_golden_sample() -> str:
    return (DESIGN_DIR / "golden_sample.html").read_text(encoding="utf-8")
