#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一认证（网页版 session + 小程序 token 并存）
==============================================
网页版走 Flask session（浏览器自动带 cookie）；
小程序没有可靠的 cookie 机制，改用 `Authorization: Bearer <token>`。

token 用 itsdangerous 的 TimestampSigner 签名，自带过期时间，
不需要额外的库、也不用往数据库写一张 token 表。

调用点统一用 app.py 里的 `current_username()`，它会依次尝试 session 与 token。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner   # noqa: E402

TOKEN_TTL_DAYS = 7
TOKEN_TTL = TOKEN_TTL_DAYS * 24 * 3600

_DEFAULT_SECRET = "agri-kg-2026-flask-secret"
_secret_cache = None


def secret_key():
    """
    签名密钥。优先环境变量，其次 config_local/config，最后用内置默认值。
    注意：正式部署请用环境变量固定下来 —— 换密钥会导致已发出的 token 全部失效。
    """
    global _secret_cache
    if _secret_cache is None:
        val = os.environ.get("APP_SECRET_KEY")
        if not val:
            try:
                import config
                val = getattr(config, "APP_SECRET_KEY", "")
            except Exception:
                val = ""
        _secret_cache = val or _DEFAULT_SECRET
    return _secret_cache


def make_token(username):
    """签发 token（内含签发时间，校验时按 TTL 判断是否过期）"""
    return TimestampSigner(secret_key()).sign(str(username).encode("utf-8")).decode("ascii")


def parse_token(token):
    """校验并解析 token，失败返回 None"""
    if not token:
        return None
    try:
        raw = TimestampSigner(secret_key()).unsign(token, max_age=TOKEN_TTL)
        name = raw.decode("utf-8").strip()
        return name or None
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    except Exception:
        return None


def bearer_from_header(value):
    """从 `Authorization: Bearer xxx` 里取出 token"""
    if not value:
        return None
    parts = str(value).split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
