"""HTML 路书渲染验证脚本（视觉验收用）：富画像 → 三主题路书 → 全页 PNG + 结构断言。

用法：python scripts/render_html_verify.py [out_prefix]
PNG 落盘 outputs/pdf_baseline/{theme}_pN.png（lushu 前缀可用自定义 out_prefix 兼容旧习惯）
断言：各主题章节标题（按主题文案）齐全；高德导航直达链接存在（删二维码未丢导航）；
打印书签定位供人工复核。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

import fitz  # noqa: E402

from tests.test_pdf_html import _rich_profile  # noqa: E402
from tripmate.pdf_html import render  # noqa: E402
from tripmate.pdf_html.themes import THEMES  # noqa: E402

prefix = sys.argv[1] if len(sys.argv) > 1 else "lushu"

bb = _rich_profile()
from test_pdf import _routes_profile_bb  # noqa: E402

bb.profile.routes = _routes_profile_bb().profile.routes  # 附路线：验证路线块 + 高德导航直达链接

for theme in THEMES:
    cfg = THEMES[theme]
    tag = theme if theme == "lushu" else f"{prefix}_{theme}" if prefix != "lushu" else theme
    out = render(bb.profile, run_id=f"verify{theme[:4]}", theme=theme)
    doc = fitz.open(out)
    for i, page in enumerate(doc):
        page.get_pixmap(dpi=100).save(rf"outputs/pdf_baseline/{tag}_p{i + 1}.png")

    # ---- 结构断言（按主题文案）----
    text = " ".join(doc[i].get_text() for i in range(len(doc)))
    links = [lk for i in range(len(doc)) for lk in doc[i].get_links() if lk.get("kind") == fitz.LINK_URI]
    amap = [lk["uri"] for lk in links if "uri.amap.com" in (lk.get("uri") or "")]
    for kw in cfg["titles"]:
        assert kw in text, f"[{theme}] 章节缺失：{kw}"
    assert amap, f"[{theme}] 高德导航直达链接缺失"
    print(f"[{theme}] pdf={out} pages={len(doc)} amap_links={len(amap)}")
    print(f"[{theme}] toc:", [(t[1], t[2]) for t in doc.get_toc()])
    doc.close()

# ---- 回退版式：无锦囊（guide_extras=None）也必须完整渲染 ----
bb2 = _rich_profile()
bb2.profile.routes = bb.profile.routes
bb2.profile.guide_extras = None
out2 = render(bb2.profile, run_id="verifyfb", theme="lushu")
doc2 = fitz.open(out2)
print(f"fallback pdf={out2} pages={len(doc2)} (无锦囊回退版式)")
doc2.close()
