"""outputs 清理守护测试（2026-09-12）：引用扫描双布局、引用/被取代/新鲜三分类、
宽限期、fail-closed（损坏会话/损坏分享索引）、空会话守卫、深度扫描兜底、apply 后状态。

全部文件钉到临时目录，不触碰真实 outputs/ 与 sessions/。"""

import json

import pytest

from tripmate import maintenance as mm


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    imgs = out / "images"
    (imgs / "crops").mkdir(parents=True, exist_ok=True)
    (out / "pdf_baseline").mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mm, "OUTPUT_DIR", out)
    monkeypatch.setattr(mm, "IMAGE_DIR", imgs)
    monkeypatch.setattr(mm, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(mm, "SHARES_PATH", tmp_path / "data" / "shares.json")
    return tmp_path


def _write_session(dirs, rel: str, *, pdf: str | None = None, images: list[str] | None = None,
                   hotel_img: str | None = None, cover: list[str] | None = None,
                   corrupt: bool = False):
    p = dirs / "sessions" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        p.write_text("{corrupted", encoding="utf-8")
        return
    profile = {"final": ({"pdf_url": f"/outputs/{pdf}", "pdf_path": f"outputs/{pdf}"} if pdf else None),
               "images": [{"image_path": f"outputs\\images\\{n}"} for n in (images or [])],
               "hotels": ([{"image_path": f"outputs\\images\\{hotel_img}"}] if hotel_img else []),
               "cover_images": list(cover or [])}
    p.write_text(json.dumps({"chat_history": [], "profile": profile}, ensure_ascii=False), encoding="utf-8")


def _mk(name: str, dirs, size: int = 10, age_days: float = 30.0):
    import os
    import time
    p = dirs / "outputs" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    old = time.time() - age_days * 86400
    os.utime(p, (old, old))
    return p


# ---- 引用扫描 ----

def test_reference_scan_both_layouts(dirs):
    _write_session(dirs, "alice/s1.json", pdf="行程计划_成都_aaa.pdf", images=["img_a1.jpg"],
                   hotel_img="hotel_x.jpg", cover=["citycover_1.jpg"])
    _write_session(dirs, "legacy.json", pdf="行程计划_成都_bbb.pdf")  # 旧扁平布局
    pdf_refs, img_refs, n = mm.collect_referenced()
    assert n == 2
    assert {"行程计划_成都_aaa.pdf", "行程计划_成都_bbb.pdf"} <= pdf_refs
    assert {"img_a1.jpg", "hotel_x.jpg", "citycover_1.jpg"} <= img_refs


def test_deep_walk_catches_unknown_field(dirs):
    """深度扫描兜底：画像里出现未知字段携带的图片路径也计入引用（防未来字段遗漏误删）。"""
    p = dirs / "sessions" / "u1" / "s1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"profile": {"final": None,
                                         "future_field": {"nested": ["outputs\\images\\newfield_img.png"]}}}),
                 encoding="utf-8")
    _, img_refs, _ = mm.collect_referenced()
    assert "newfield_img.png" in img_refs


# ---- fail-closed ----

def test_corrupt_session_aborts_cleanup(dirs):
    _write_session(dirs, "alice/s1.json", pdf="行程计划_成都_aaa.pdf")
    _write_session(dirs, "alice/bad.json", corrupt=True)
    with pytest.raises(mm.MaintenanceAborted, match="损坏"):
        mm.plan_cleanup()


def test_corrupt_shares_aborts_cleanup(dirs):
    _write_session(dirs, "alice/s1.json", pdf="行程计划_成都_aaa.pdf")
    (dirs / "data" / "shares.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(mm.MaintenanceAborted, match="分享索引"):
        mm.plan_cleanup()


# ---- 计划三分类与宽限期 ----

def test_plan_keeps_referenced_deletes_orphan_with_grace(dirs):
    _write_session(dirs, "alice/s1.json", pdf="行程计划_成都_keep.pdf", images=["keep_img.jpg"])
    (dirs / "data" / "shares.json").write_text(
        json.dumps({"sh1": {"user": "alice", "sid": "t1", "file": "行程计划_成都_shared.pdf"}}),
        encoding="utf-8")
    keep_pdf = _mk("行程计划_成都_keep.pdf", dirs)          # 被会话引用 → 永留
    shared_pdf = _mk("行程计划_成都_shared.pdf", dirs)       # 被分享引用 → 永留
    old_orphan = _mk("行程计划_成都_old.pdf", dirs)          # 无引用 + 30 天 → 删
    fresh_orphan = _mk("行程计划_成都_fresh.pdf", dirs, age_days=0.5)  # 无引用但 0.5 天 → 宽限内留
    keep_img = _mk("images/keep_img.jpg", dirs)              # 被画像引用 → 永留
    orphan_img = _mk("images/orphan.jpg", dirs)              # 无引用 + 30 天 → 删
    fresh_img = _mk("images/fresh.jpg", dirs, age_days=0.2)  # 无引用但 0.2 天 → 保护窗内留
    crop = _mk("images/crops/e.png", dirs)                   # 缓存 → 删
    baseline = _mk("pdf_baseline/p1.png", dirs)              # 预览 → 删
    tmp = _mk("行程计划_成都_x.tmp.pdf", dirs)               # 崩溃残留 → 删
    old_log = _mk("_e2e_run.log", dirs)                      # 旧日志 → 删

    plan = mm.plan_cleanup(pdf_grace_days=3, min_age_days=1)

    assert keep_pdf not in plan.superseded_pdfs and shared_pdf not in plan.superseded_pdfs
    assert old_orphan in plan.superseded_pdfs and fresh_orphan not in plan.superseded_pdfs
    assert tmp not in plan.superseded_pdfs   # tmp 残留只进 tmp_residue，不与 PDF 扫描双重计数
    assert keep_img not in plan.orphan_images and orphan_img in plan.orphan_images
    assert fresh_img not in plan.orphan_images
    assert crop in plan.crops and baseline in plan.baseline
    assert tmp in plan.tmp_residue and old_log in plan.old_logs
    assert plan.no_sessions_guard is False

    result = mm.apply(plan)
    assert keep_pdf.exists() and shared_pdf.exists() and keep_img.exists()
    assert not old_orphan.exists() and not orphan_img.exists() and not crop.exists()
    assert not baseline.exists() and not tmp.exists() and not old_log.exists()
    assert fresh_orphan.exists() and fresh_img.exists()   # 保护窗内未删
    assert result.freed_bytes > 0


def test_no_sessions_guard_skips_pdf_cleanup(dirs):
    """空会话守卫：一个会话文件都没有 → 跳过 PDF 清理（防全新环境把引用集为空误判成全部无引用）。"""
    pdf = _mk("行程计划_成都_lone.pdf", dirs)
    plan = mm.plan_cleanup()
    assert plan.no_sessions_guard is True
    assert plan.superseded_pdfs == []
    mm.apply(plan)
    assert pdf.exists()


def test_revoked_share_pdf_not_protected(dirs):
    _write_session(dirs, "alice/s1.json", pdf="行程计划_成都_keep.pdf")
    (dirs / "data" / "shares.json").write_text(
        json.dumps({"sh1": {"user": "alice", "sid": "t1", "file": "行程计划_成都_dead.pdf",
                            "revoked": True}}),
        encoding="utf-8")
    dead = _mk("行程计划_成都_dead.pdf", dirs)
    plan = mm.plan_cleanup()
    assert dead in plan.superseded_pdfs   # 已撤销分享不再保护该 PDF
