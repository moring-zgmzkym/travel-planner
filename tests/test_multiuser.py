"""多用户版守护测试（2026-09-12）：注册/登录/fail-closed、用户名校验、会话隔离、
PDF 鉴权交付、分享直链、偏好记忆、MCP 池并发隔离。

HTTP 级用 httpx AsyncClient + ASGITransport 直连 FastAPI app（本项目 httpx 版本的
ASGITransport 仅支持异步客户端）；账号库/密钥/分享/记忆/会话目录全部钉到临时目录，
不触碰开发者真实 data/ 与 sessions/。"""

import asyncio

import httpx
import pytest

from tripmate.gateway import app as gw
from tripmate.models import FinalDelivery
from tripmate.tools import mcp_client as mcp


# ---- 夹具：所有持久化钉到临时目录 ----

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("tripmate.auth.USERS_PATH", tmp_path / "users.json")
    monkeypatch.setattr("tripmate.auth.SECRET_PATH", tmp_path / "secret.key")
    monkeypatch.setattr(gw, "_SHARES_PATH", tmp_path / "shares.json")
    monkeypatch.setattr("tripmate.memory.DATA_DIR", tmp_path / "mem")
    monkeypatch.setattr("tripmate.persistence.SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(gw, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(gw, "OUTPUT_DIR", tmp_path / "outputs")  # PDF 交付/分享用临时产物目录（不依赖真实 outputs）
    gw.sessions.clear()
    yield tmp_path
    gw.sessions.clear()


def _write_fake_pdf(name: str) -> None:
    """在被隔离的 OUTPUT_DIR 写一个最小 PDF 头文件（gw.OUTPUT_DIR 已由夹具钉到临时目录）。"""
    p = gw.OUTPUT_DIR / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4 fake-for-test")


def _run(coro):
    return asyncio.run(coro)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://t")


async def _register(c: httpx.AsyncClient, name: str, pw: str = "pass123") -> dict:
    r = await c.post("/api/register", json={"username": name, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- 注册 / 登录 / 令牌 ----

def test_register_login_me_flow(isolated):
    async def main():
        async with _client() as c:
            first = await _register(c, "alice")
            assert first["is_admin"] is True and first["token"]
            second = await _register(c, "bob")
            assert second["is_admin"] is False
            r = await c.post("/api/login", json={"username": "alice", "password": "pass123"})
            assert r.status_code == 200 and r.json()["token"]
            r = await c.get("/api/me", headers=_auth(first["token"]))
            assert r.status_code == 200 and r.json()["username"] == "alice"
    _run(main())


def test_register_duplicate_and_wrong_password(isolated):
    async def main():
        async with _client() as c:
            await _register(c, "alice")
            r = await c.post("/api/register", json={"username": "alice", "password": "pass123"})
            assert r.status_code == 400
            r = await c.post("/api/login", json={"username": "alice", "password": "wrong!!"})
            assert r.status_code == 400
    _run(main())


def test_username_reserved_and_traversal_blocked(isolated):
    async def main():
        async with _client() as c:
            for bad in ("CON", "../evil", "a/b", "a b"):
                r = await c.post("/api/register", json={"username": bad, "password": "pass123"})
                assert r.status_code == 400, bad
    _run(main())


def test_users_json_corruption_fails_closed(isolated):
    """账号库损坏：注册与登录一律拒绝（fail-closed），绝不按空库静默重建管理员。"""
    isolated.joinpath("users.json").write_text("{corrupted", encoding="utf-8")

    async def main():
        async with _client() as c:
            r = await c.post("/api/register", json={"username": "alice", "password": "pass123"})
            assert r.status_code == 400 and "损坏" in r.json()["detail"]
            r = await c.post("/api/login", json={"username": "alice", "password": "pass123"})
            assert r.status_code == 400 and "损坏" in r.json()["detail"]
    _run(main())


def test_api_requires_auth(isolated):
    async def main():
        async with _client() as c:
            assert (await c.get("/api/sessions")).status_code == 401
            assert (await c.post("/api/sessions")).status_code == 401
            assert (await c.get("/api/profile")).status_code == 401
            assert (await c.get("/api/usage")).status_code == 401
            assert (await c.get("/api/pdf")).status_code == 401
    _run(main())


def test_short_lived_ticket_binds_username(isolated):
    from tripmate import auth as au

    async def main():
        async with _client() as c:
            tok = (await _register(c, "alice"))["token"]
            r = await c.post("/api/ws-ticket", headers=_auth(tok))
            assert r.status_code == 200
            assert au.verify_ws_ticket(r.json()["ticket"]) == "alice"
    _run(main())
    assert au.verify_ws_ticket("garbage") is None


# ---- 会话隔离 ----

def test_session_isolation_between_users(isolated):
    async def main():
        async with _client() as c:
            alice = await _register(c, "alice")
            bob = await _register(c, "bob")
            r = await c.post("/api/sessions", headers=_auth(alice["token"]))
            assert r.status_code == 200
            alice_sid = r.json()["sid"]
            r = await c.get("/api/sessions", headers=_auth(alice["token"]))
            assert [s["sid"] for s in r.json()] == [alice_sid]
            # bob 看不到 alice 的会话
            r = await c.get("/api/sessions", headers=_auth(bob["token"]))
            assert r.json() == []
            # bob 用同一 sid 访问拿到的是自己的全新会话（画像为空），而非 alice 的数据
            r = await c.get("/api/profile", params={"sid": alice_sid}, headers=_auth(bob["token"]))
            assert r.status_code == 200 and not r.json()["basic_info"].get("destination")
    _run(main())


# ---- PDF 鉴权交付与分享 ----

def _session_with_final(username: str, sid: str, pdf_name: str = "行程计划_成都_insuranc.pdf"):
    from tripmate.session import Session
    key = gw._skey(username, sid)
    sess = Session(gw._sid_of(key), username=username)
    sess.bb.profile.final = FinalDelivery(pdf_url=f"/outputs/{pdf_name}",
                                          order_summary=[], total_price=1000)
    gw.sessions[key] = sess
    return sess


def test_pdf_route_ownership(isolated):
    _write_fake_pdf("行程计划_成都_insuranc.pdf")
    _session_with_final("alice", "t1")

    async def main():
        async with _client() as c:
            alice = await _register(c, "alice")
            bob = await _register(c, "bob")
            r = await c.get("/api/pdf", params={"sid": "t1"}, headers=_auth(alice["token"]))
            assert r.status_code == 200 and r.content[:4] == b"%PDF"
            # bob 同 sid 访问 → 自己的空会话（无定稿）→ 404，拿不到 alice 的 PDF
            r = await c.get("/api/pdf", params={"sid": "t1"}, headers=_auth(bob["token"]))
            assert r.status_code == 404
    _run(main())


def test_share_flow_public_access_and_revoke(isolated):
    _write_fake_pdf("行程计划_成都_insuranc.pdf")
    _session_with_final("alice", "t1")

    async def main():
        async with _client() as c:
            alice = await _register(c, "alice")
            header = _auth(alice["token"])
            r = await c.post("/api/share", json={"sid": "t1"}, headers=header)
            assert r.status_code == 200
            share_id, url = r.json()["share_id"], r.json()["url"]
            # 幂等：重复创建复用同一链接
            r2 = await c.post("/api/share", json={"sid": "t1"}, headers=header)
            assert r2.json()["share_id"] == share_id
            # 免登录直链可下载
            r = await c.get(url)
            assert r.status_code == 200 and r.content[:4] == b"%PDF"
            # 无定稿的会话不能创建分享
            r = await c.post("/api/share", json={"sid": "nope"}, headers=header)
            assert r.status_code == 404
            # 他人不能撤销；本人撤销后直链 404
            bob = await _register(c, "bob")
            r = await c.delete(f"/api/share/{share_id}", headers=_auth(bob["token"]))
            assert r.status_code == 404
            r = await c.delete(f"/api/share/{share_id}", headers=header)
            assert r.status_code == 200
            assert (await c.get(url)).status_code == 404
    _run(main())


def test_share_id_traversal_rejected(isolated):
    async def main():
        async with _client() as c:
            assert (await c.get("/share/..%2F..%2Fusers")).status_code == 404
            assert (await c.get("/share/short")).status_code == 404
    _run(main())


# ---- 偏好记忆 ----

def test_memory_store_and_prompt_injection(isolated):
    from tripmate import memory as mm

    mm.save_memory("alice", {"preferences": mm.build_memory_items(["酒店偏好：春熙路商圈 300-500 元/晚"]),
                             "trips": []})
    text = mm.prefs_prompt_text("alice")
    assert "春熙路" in text and "永远优先" in text
    assert mm.prefs_prompt_text("bob") == ""          # 无偏好 → 空串（提示词零变化）
    assert mm.prefs_prompt_text("") == ""             # 无用户 → 空串
    prefs = mm.load_memory("alice")["preferences"]
    assert mm.delete_preference("alice", prefs[0]["id"]) is True
    assert mm.load_memory("alice")["preferences"] == []
    mm.clear_memory("alice")
    assert mm.load_memory("alice") == {"preferences": [], "trips": []}


def test_memory_capture_merges_and_skips_garbage(isolated, monkeypatch):
    """提炼合并：LLM 返回新偏好列表 → 覆盖写盘；畸形/空输出 → 不动旧档案。"""
    import types
    from tripmate import memory as mm
    from tripmate.models import BasicInfo, TravelProfile

    class FakeClient:
        def __init__(self, reply):
            self.reply = reply

        async def create(self, messages):
            return types.SimpleNamespace(content=self.reply)

    prof = TravelProfile(basic_info=BasicInfo(destination="成都", budget=6000))
    monkeypatch.setattr(mm, "get_model_client", lambda: FakeClient(
        '{"preferences": ["预算习惯：单次 6000 元内", "风格：休闲吃吃喝喝"]}'))
    assert _run(mm.capture_from_profile("alice", prof)) is True
    prefs = mm.load_memory("alice")["preferences"]
    assert len(prefs) == 2 and prefs[0]["text"].startswith("预算习惯")

    monkeypatch.setattr(mm, "get_model_client", lambda: FakeClient('前置说明 {"preferences": []} 后缀'))
    assert _run(mm.capture_from_profile("alice", prof)) is False   # 空提炼 → 保留旧档案
    assert len(mm.load_memory("alice")["preferences"]) == 2
    monkeypatch.setattr(mm, "get_model_client", lambda: FakeClient('完全不是 JSON'))
    assert _run(mm.capture_from_profile("alice", prof)) is False
    assert len(mm.load_memory("alice")["preferences"]) == 2


def test_memory_http_endpoints(isolated):
    from tripmate import memory as mm
    mm.save_memory("alice", {"preferences": mm.build_memory_items(["风格：休闲"]), "trips": []})

    async def main():
        async with _client() as c:
            tok = (await _register(c, "alice"))["token"]
            r = await c.get("/api/memory", headers=_auth(tok))
            assert r.status_code == 200 and len(r.json()["preferences"]) == 1
            pref_id = r.json()["preferences"][0]["id"]
            r = await c.delete(f"/api/memory/{pref_id}", headers=_auth(tok))
            assert r.json()["ok"] is True
            r = await c.delete("/api/memory", headers=_auth(tok))
            assert r.json()["ok"] is True
    _run(main())


# ---- MCP 池并发隔离（多用户硬前提） ----

class _FakePooledSession:
    """池回收会调 close()：测试桩须与 PersistentMcpSession 的该接口同形。"""

    def __init__(self, name: str = "fake"):
        self.name = name

    async def close(self) -> None:
        return None


def test_mcp_pool_isolated_between_concurrent_runs():
    """两个并发规划 run 各持各的池：A 放入的会话对 B 不可见，互不覆盖。"""

    async def main():
        mcp.begin_mcp_pool()
        seen = {}

        async def run_a():
            mcp.begin_mcp_pool()
            fake = _FakePooledSession("a")
            mcp._pool_put("amap", fake)
            await asyncio.sleep(0.02)
            seen["a"] = mcp._pool_get("amap")
            await mcp.end_mcp_pool()

        async def run_b():
            mcp.begin_mcp_pool()
            await asyncio.sleep(0.01)
            seen["b"] = mcp._pool_get("amap")
            await mcp.end_mcp_pool()

        await asyncio.gather(run_a(), run_b())
        return seen

    seen = asyncio.run(main())
    assert isinstance(seen["a"], _FakePooledSession) and seen["b"] is None  # B 的池里没有 A 的会话


# ---- 团队真实构建守护（堵住"_build_team 从未被测试执行"的盲区） ----

def test_build_team_smoke_with_memory_prompt(isolated, monkeypatch):
    """真实构建 SelectorGroupChat：Planner 提示词应携带该用户偏好记忆。
    此前 memory 注入把局部函数误写成 self.属性，团队一启动就崩且测试全绿——本测试堵住该盲区。"""
    import types

    from tripmate import memory as mm
    from tripmate.session import Session

    mm.save_memory("alice", {"preferences": mm.build_memory_items(["风格：休闲，少赶路"]), "trips": []})
    monkeypatch.setattr("tripmate.session.save_session", lambda *a, **k: None)
    s = Session("t-build", username="alice")

    async def main():
        ctx = types.SimpleNamespace(bb=s.bb, bus=s.bus, state=s.runner._state,
                                    jobs=s.runner._jobs, runner=s.runner, run_id="t")
        team = s.runner._build_team(ctx, "collect")
        assert team is not None
        planner = next(a for a in team._participants if a.name == "Planner")
        sm_text = "".join(getattr(m, "content", "") for m in planner._system_messages)
        assert "风格：休闲，少赶路" in sm_text

    asyncio.run(main())
