"""定向诊断（2026-09-19 e2e 后）：structure_guide_rows 结构化失败与 search_city_covers
0 候选的真实通道行为。只查一次 Tavily（8 路）+ 一次结构化 LLM + 一次封面检索，量小。
回退原因看 logs/tripmate.log 中 digest logger 的 WARNING/INFO 行。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tripmate.status  # noqa: F401,E402  — 触发 FileHandler 装配，digest logger 行才落盘
from tripmate.digest import structure_guide_rows  # noqa: E402
from tripmate.tools.search import search_city_covers, search_guides  # noqa: E402


async def main() -> None:
    print("=== ① 攻略 8 路检索（真实 Tavily）===")
    r = await search_guides("成都", "十一", style_hint="休闲 美食")
    print("mode:", r["mode"], "| rows:", len(r["digest"]))
    for d in r["digest"]:
        print(f"  - {d['source_name']} | raw_answer {len(d.get('raw_answer') or '')} 字 | "
              f"标题 {len(d.get('raw_titles') or [])} 条")

    print("\n=== ② 结构化（真实 LLM，回退原因见日志）===")
    rows = await structure_guide_rows(r["digest"], "成都")
    filled = sum(1 for d in rows if d.get("spots") or d.get("foods") or d.get("routes") or d.get("warnings"))
    print(f"结构化产出: {filled}/{len(rows)} 行")
    for d in rows[:3]:
        print("  -", d.get("source_name"), "| spots:", d.get("spots"), "| foods:", d.get("foods"))

    print("\n=== ③ 封面图（真实 Tavily include_images）===")
    covers = await search_city_covers("成都")
    print("合格封面:", len(covers), covers[:2])


if __name__ == "__main__":
    asyncio.run(main())
