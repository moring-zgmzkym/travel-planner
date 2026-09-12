"""多用户账号与令牌（局域网多用户版，2026-09-12）。

- 口令：PBKDF2-HMAC-SHA256（200k 轮 + 16B 随机盐），只存哈希；标准库实现，零新依赖
- 令牌：HMAC-SHA256 签名 `{username}:{exp}`（30 天），无状态校验；首启自动生成 data/secret.key
- WS 票据：30 秒短时票据——浏览器 WebSocket 无法带 Authorization 头，用票据替代长期令牌进 URL，
  避免令牌泄漏到访问日志；票据绑定用户名、到期即失效（局域网威胁模型下不做事努性消费表）
- fail-closed：users.json 已存在但损坏 → 拒绝注册与登录并报错，绝不按空库重建
  （防服务异常导致管理员被静默重置、账号被抢占）
- 用户名硬校验：字符集白名单 + Windows 保留设备名黑名单（用户名即持久化目录名）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from .config import DATA_DIR

USERS_PATH = DATA_DIR / "users.json"
SECRET_PATH = DATA_DIR / "secret.key"

_PBKDF2_ROUNDS = 200_000
TOKEN_TTL_S = 30 * 24 * 3600          # 登录令牌 30 天
WS_TICKET_TTL_S = 30                  # WS 握手票据 30 秒

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5-]{2,24}$")
# Windows 保留设备名：用户名会成为 sessions/{用户名}/ 目录名，这些名字在 Win32 下是设备而非文件
_RESERVED_NAMES = ({"con", "prn", "aux", "nul"}
                   | {f"com{i}" for i in range(1, 10)}
                   | {f"lpt{i}" for i in range(1, 10)})

_LOCK = asyncio.Lock()                # 注册/登录的读改写串行化（文件小，事件循环线程直写可接受）


class AuthError(Exception):
    """携带用户可读信息的认证错误（网关转 400/401）。"""


# ---- 密钥与存储 ----

def _secret() -> bytes:
    """HMAC 签名密钥：首启自动生成；丢失=全体令牌失效需重新登录（无害）。"""
    if not SECRET_PATH.exists():
        SECRET_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
    return SECRET_PATH.read_text(encoding="utf-8").strip().encode()


def _load_users() -> dict:
    """读账号库。文件不存在 → 空库；存在但损坏 → 抛 AuthError（fail-closed，绝不重建）。"""
    if not USERS_PATH.exists():
        return {"users": {}}
    try:
        data = json.loads(USERS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise ValueError("结构不符")
        return data
    except (OSError, ValueError) as e:
        raise AuthError(f"账号库损坏（{e}），已拒绝注册/登录；请管理员检查 data/users.json") from e


def _save_users(users: dict) -> None:
    tmp = USERS_PATH.with_name(USERS_PATH.name + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, USERS_PATH)


# ---- 校验与哈希 ----

def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not _USERNAME_RE.match(name):
        raise AuthError("用户名需 2-24 位，仅限中英文、数字、下划线、连字符")
    if name.lower() in _RESERVED_NAMES:
        raise AuthError("该用户名是系统保留名，请换一个")
    return name


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS).hex()


# ---- 注册 / 登录 ----

async def register(username: str, password: str) -> dict:
    """注册新用户。返回 {"username", "token", "is_admin"}；首个注册者自动成为管理员。"""
    name = validate_username(username)
    if len(password or "") < 6:
        raise AuthError("密码至少 6 位")
    async with _LOCK:
        users = _load_users()
        if name in users["users"]:
            raise AuthError("用户名已被注册")
        is_admin = not users["users"]  # 首个注册者 = 管理员
        salt = secrets.token_bytes(16)
        users["users"][name] = {
            "salt": salt.hex(),
            "hash": _hash_password(password, salt),
            "is_admin": is_admin,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_users(users)
    return {"username": name, "token": _issue_token(name), "is_admin": is_admin}


async def login(username: str, password: str) -> dict:
    name = validate_username(username)
    async with _LOCK:
        users = _load_users()
        info = users["users"].get(name)
    if not info or not hmac.compare_digest(info["hash"],
                                           _hash_password(password or "", bytes.fromhex(info["salt"]))):
        raise AuthError("用户名或密码不正确")
    return {"username": name, "token": _issue_token(name),
            "is_admin": bool(info.get("is_admin"))}


async def change_password(username: str, old_password: str, new_password: str) -> None:
    if len(new_password or "") < 6:
        raise AuthError("新密码至少 6 位")
    async with _LOCK:
        users = _load_users()
        info = users["users"].get(username)
        if not info or not hmac.compare_digest(info["hash"],
                                               _hash_password(old_password or "", bytes.fromhex(info["salt"]))):
            raise AuthError("原密码不正确")
        salt = secrets.token_bytes(16)
        info["salt"], info["hash"] = salt.hex(), _hash_password(new_password, salt)
        _save_users(users)


def admin_reset_password(username: str, new_password: str) -> None:
    """管理员命令行重置密码（绕开旧口令；供忘记密码场景，见启动指南）。"""
    users = _load_users()
    if username not in users["users"]:
        raise AuthError("用户不存在")
    salt = secrets.token_bytes(16)
    users["users"][username]["salt"] = salt.hex()
    users["users"][username]["hash"] = _hash_password(new_password, salt)
    _save_users(users)


# ---- 令牌 / 票据 ----

def _issue_token(username: str) -> str:
    payload = f"{username}:{int(time.time()) + TOKEN_TTL_S}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    raw = f"{payload}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_token(token: str) -> str | None:
    """校验令牌，返回用户名；无效/过期返回 None（绝不抛异常）。"""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        username, exp, sig = raw.rsplit(":", 2)
        payload = f"{username}:{exp}"
        expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect) or int(exp) < time.time():
            return None
        if not _USERNAME_RE.match(username):
            return None
        return username
    except Exception:  # noqa: BLE001 — 任何畸形令牌一律按未认证处理
        return None


def issue_ws_ticket(username: str) -> str:
    payload = f"{username}:{int(time.time()) + WS_TICKET_TTL_S}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def verify_ws_ticket(ticket: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(ticket + "=" * (-len(ticket) % 4)).decode()
        username, exp, sig = raw.rsplit(":", 2)
        payload = f"{username}:{exp}"
        expect = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect) or int(exp) < time.time():
            return None
        return username
    except Exception:  # noqa: BLE001
        return None


def user_info(username: str) -> dict | None:
    users = _load_users()
    info = users["users"].get(username)
    if not info:
        return None
    return {"username": username, "is_admin": bool(info.get("is_admin"))}
