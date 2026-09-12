"""共享夹具：把 PDF/图片类测试的产物钉到 pytest 临时目录。

根因修复（2026-09-12）：此前 build_pdf / reportlab 模板 / 占位图生成直接写真实 outputs/，
每跑一次 pytest 就在真实目录刷新一批测试 PDF 与图片（用户视角即"outputs 文件越积越多"）。
autouse 全局生效；断言 `Path(path).parent.name == "outputs"` 的用例不受影响（临时目录同名）。
注意：不钉 maintenance——其测试自带目录隔离夹具。"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_outputs(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    (out / "images" / "crops").mkdir(parents=True)
    monkeypatch.setattr("tripmate.pdf_html.context.OUTPUT_DIR", out)
    monkeypatch.setattr("tripmate.pdf_templates.base.OUTPUT_DIR", out)
    monkeypatch.setattr("tripmate.pdf_templates.base.CROP_DIR", out / "images" / "crops")
    monkeypatch.setattr("tripmate.tools.imagegen.IMAGE_DIR", out / "images")
