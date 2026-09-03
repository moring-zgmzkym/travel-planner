"""PDF 渲染验证脚本（临时验收用）：富画像 → 指定模板渲染 → 全页 PNG。

用法：python scripts/render_verify.py <template> <out_prefix>
模板可选 classic / cartoon / 其余注册名；PNG 落盘 outputs/pdf_baseline/<out_prefix>_pN.png
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))

import fitz  # noqa: E402

from tripmate.models import FoodNote, SpotNote  # noqa: E402
from tripmate.pdf_gen import build_pdf  # noqa: E402
from tripmate.tools.imagegen import generate_placeholder  # noqa: E402

template = sys.argv[1] if len(sys.argv) > 1 else "cartoon"
prefix = sys.argv[2] if len(sys.argv) > 2 else template

exec(Path("tests/test_pdf.py").read_text(encoding="utf-8").split("def test_build_pdf_smoke")[0])

import glob  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

real = []
for p in glob.glob("outputs/images/*.jpg"):
    try:
        with PILImage.open(p) as im:
            if im.size[0] >= 1200 and im.size[0] > im.size[1]:
                real.append(p)
    except Exception:
        pass

bb = _profile_bb()
bb.profile.basic_info.travel_dates = ["2026-09-04", "2026-09-05"]
bb.profile.draft.days[0].date = "2026-09-04"
bb.profile.draft.days[1].date = "2026-09-05"
bb.profile.cover_images = real[:3]
bb.profile.weather = {"city": "成都", "source": "Open-Meteo（真实预报）", "reference_only": False, "days": [
    {"date": "2026-09-04", "day_text": "多云转晴", "temp_min": 20, "temp_max": 31},
    {"date": "2026-09-05", "day_text": "中雨", "temp_min": 19, "temp_max": 26}]}
bb.profile.spot_notes = [
    SpotNote(name="大熊猫繁育研究基地", intro="成都名片，近距离看大熊猫幼崽", activities="赶早开园即入，优先看月亮产房"),
    SpotNote(name="宽窄巷子", intro="清代川西民居街区", activities="盖碗茶、掏耳、逛文创小店")]
bb.profile.food_notes = [
    FoodNote(name="火锅", intro="牛油九宫格锅底，毛肚黄喉七上八下", image_path=generate_placeholder("火锅")),
    FoodNote(name="串串香", intro="竹签串菜红汤涮煮，按签计数", image_path=generate_placeholder("串串香"))]
bb.profile.hotels[0].image_path = generate_placeholder("全季酒店成都春熙路店")
from tripmate.models import HotelCandidate  # noqa: E402
for i, (n, p, d, r) in enumerate([("亚朵酒店（天府广场店）", 488, 1.2, 4.8),
                                  ("如家精选（春熙路店）", 319, 0.8, 4.4)], 2):
    bb.profile.hotels.append(HotelCandidate(
        name=n, price_per_night=p, distance_km=d, rating=r,
        link="https://hotels.ctrip.com/x", score=round(0.9 - 0.06 * i, 3), selected=False,
        reason="评分次优备选", source="Dida 酒店 MCP（实时数据）", reference_only=False,
        image_path=generate_placeholder(n)))

out = build_pdf(bb.profile, run_id=f"verify{prefix[:6]}", template=template)
doc = fitz.open(out)
for i, page in enumerate(doc):
    page.get_pixmap(dpi=100).save(rf"outputs/pdf_baseline/{prefix}_p{i+1}.png")
print(f"template={template} pages={len(doc)} prefix={prefix}")
doc.close()
