"""Designer 全链路样张生成（无 LLM）：以金样为"优秀模型输出"走完确定性链路。

验证：write_html 消毒套壳 → render_pdf 渲染诊断 → 缓存写入 → PDF 产物。
产物：outputs/行程计划_成都_designer_sample_ai.pdf + outputs/html/ 下 HTML 源。
运行：python scripts/designer_sample.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from tripmate import designer as dmod  # noqa: E402
from tripmate.design import load_golden_sample  # noqa: E402
from tripmate.models import ImageItem, TravelProfile  # noqa: E402
from tripmate.tools.imagegen import generate_placeholder  # noqa: E402


def _profile() -> TravelProfile:
    """成都样例画像（对应 tests/test_pdf.py fixture，图占位为本地示意配图）。"""
    sys.path.insert(0, str(BASE_DIR / "tests"))
    from test_pdf import _profile_bb

    bb = _profile_bb()
    prof: TravelProfile = bb.profile
    prof.basic_info.template = "designer"
    return prof


class _GoldenAgent:
    """假 Agent：像优秀模型一样调用 write_html（金样替换真实图片 URI）+ render_pdf。"""

    def __init__(self, tools: list, html: str):
        self._tools = {t.__name__: t for t in tools}
        self._html = html

    async def run(self, task: str = "") -> None:
        await self._tools["write_html"](self._html)
        report = await self._tools["render_pdf"]()
        print("render_pdf →", report[:220])


async def main() -> None:
    prof = _profile()
    # 金样中的占位 URI → 真实存在的本地示意配图 URI（file:///），按图注一一对应
    imgs = {s: Path(generate_placeholder(s)).resolve().as_uri() for s in
            ("大熊猫基地", "宽窄巷子", "锦里古街", "武侯祠", "人民公园", "春熙路太古里")}
    fake_to_spot = {
        "7d44de1b90384afa": "春熙路太古里",   # 封面
        "11869a583f75b47f": "大熊猫基地",     # 图墙 1
        "459413fa7eb67d28": "宽窄巷子",       # 图墙 2
        "65d8439100a2f880": "锦里古街",       # 图墙 3
    }
    body = load_golden_sample()
    for fake, spot in fake_to_spot.items():
        body = re.sub(rf'file:///D:/placeholder/outputs/images/{fake}[^"\']*', imgs[spot], body)
    assert "file:///D:/placeholder" not in body, "金样占位 URI 未全部替换"

    run_id = "designer_sample"
    factory = lambda system_prompt, tools: _GoldenAgent(tools, body)  # noqa: E731
    result = await dmod.designer_chain(prof, run_id, factory, system_prompt="(金样直出)",
                                       budget_check=lambda: None)
    print(f"\n[chain] attempts={result.attempts} engine={result.engine} "
          f"cache={result.from_cache} findings={len(result.findings)}")
    print(f"[chain] html  = {result.html_path}")
    print(f"[chain] pdf   = {result.pdf_path}")

    # 缓存命中复验：同画像第二次定稿应直接复用 HTML 重渲染
    def _spy_factory(system_prompt, tools):
        raise AssertionError("缓存命中不应重新生成")

    result2 = await dmod.designer_chain(prof, run_id, _spy_factory, system_prompt="x",
                                        budget_check=lambda: None)
    print(f"[cache] from_cache={result2.from_cache} pdf={result2.pdf_path}")
    assert result2.from_cache, "同画像重复定稿应命中缓存"


if __name__ == "__main__":
    asyncio.run(main())
