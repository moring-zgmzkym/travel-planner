"""样张生成：用同一画像渲染注册表全部模板，导出首页+内页 PNG 供小组评审（任务 2.6）。

用法：python scripts/make_template_samples.py
产物：docs/references/samples/<模板名>_cover.png / <模板名>_page2.png + 同名 PDF。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # PyMuPDF  # noqa: E402

from tests.test_pdf import _profile_bb  # noqa: E402
from tripmate.pdf_templates import REGISTRY  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "references" / "samples"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, tpl in sorted(REGISTRY.items()):
        bb = _profile_bb()
        pdf_path = Path(tpl.render(bb.profile, run_id=f"sample_{name}"))
        doc = fitz.open(pdf_path)
        for idx, tag in ((0, "cover"), (1, "page2")):
            if idx >= len(doc):
                continue
            pix = doc[idx].get_pixmap(dpi=100)
            png = OUT / f"{name}_{tag}.png"
            pix.save(str(png))
            print(f"[ok] {png.name}")
        doc.close()
        print(f"[ok] {pdf_path.name}（{tpl.display_name}，{len(fitz.open(pdf_path))} 页）")
    print(f"\n样张目录：{OUT}")


if __name__ == "__main__":
    main()
