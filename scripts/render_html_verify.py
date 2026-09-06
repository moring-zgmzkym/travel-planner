"""HTML 路书渲染验证脚本（视觉验收用）：富画像 → 唐风路书 → 全页 PNG。

用法：python scripts/render_html_verify.py [out_prefix]
PNG 落盘 outputs/pdf_baseline/<prefix 或 html>_pN.png（默认前缀 html）
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

import fitz  # noqa: E402

from tests.test_pdf_html import _rich_profile  # noqa: E402
from tripmate.pdf_html import render  # noqa: E402

prefix = sys.argv[1] if len(sys.argv) > 1 else "html"

bb = _rich_profile()
from test_pdf import _routes_profile_bb  # noqa: E402

bb.profile.routes = _routes_profile_bb().profile.routes  # 附路线：验证路线块 + 导航二维码
out = render(bb.profile, run_id=f"verify{prefix[:6]}")
doc = fitz.open(out)
for i, page in enumerate(doc):
    page.get_pixmap(dpi=100).save(rf"outputs/pdf_baseline/{prefix}_p{i + 1}.png")
print(f"html pdf={out} pages={len(doc)} prefix={prefix}")
doc.close()
