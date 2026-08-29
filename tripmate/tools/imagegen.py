"""本地示意配图生成（降级通道）：PIL 渐变卡片，明确标注「示意配图 · 非实景」。"""

from __future__ import annotations

import hashlib

from PIL import Image, ImageDraw, ImageFont

from ..config import IMAGE_DIR

_SIZE = (800, 500)
_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for p in _FONTS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return None


def generate_placeholder(spot: str) -> str:
    """按景点名生成稳定配色的示意卡片。返回本地路径。"""
    name = hashlib.md5(spot.encode()).hexdigest()[:16] + ".png"
    path = IMAGE_DIR / name
    if path.exists():
        return str(path)

    h = int(hashlib.md5(spot.encode()).hexdigest(), 16)
    hue = h % 360
    import colorsys
    c1 = colorsys.hls_to_rgb(hue / 360, 0.62, 0.45)
    c2 = colorsys.hls_to_rgb(((hue + 40) % 360) / 360, 0.32, 0.5)
    img = Image.new("RGB", _SIZE)
    d = ImageDraw.Draw(img)
    for y in range(_SIZE[1]):
        t = y / _SIZE[1]
        color = tuple(int(a + (b - a) * t) for a, b in zip(
            (int(c1[0] * 255), int(c1[1] * 255), int(c1[2] * 255)),
            (int(c2[0] * 255), int(c2[1] * 255), int(c2[2] * 255))))
        d.line([(0, y), (_SIZE[0], y)], fill=color)

    f_small = _font(28)
    # 标题自适应字号：超长景点名（如分隔失败把整串清单当一名）缩字号至卡片宽度内，避免左右裁切
    size = 64
    f_big = _font(size)
    while f_big and d.textlength(spot, font=f_big) > _SIZE[0] * 0.88 and size > 24:
        size -= 4
        f_big = _font(size)
    if f_big:
        w = d.textlength(spot, font=f_big)
        d.text(((_SIZE[0] - w) / 2, 190), spot, fill="white", font=f_big)
    if f_small:
        for i, line in enumerate(("示意配图 · 非实景", "TripMate 模拟数据模式")):
            w = d.textlength(line, font=f_small)
            d.text(((_SIZE[0] - w) / 2, 300 + i * 44), line, fill=(240, 240, 240), font=f_small)

    img.save(path)
    return str(path)
