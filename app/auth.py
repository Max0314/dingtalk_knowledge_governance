"""DingTalk login and session guard, modeled on bi_center's auth design.

Web flow (works in any browser, including DingTalk's):
  GET /api/auth/login-url  -> login.dingtalk.com/oauth2/auth (state is HMAC-signed)
  GET /api/auth/dingtalk/callback?authCode&state
      -> POST /v1.0/oauth2/userAccessToken (clientId/clientSecret/code)
      -> GET  /v1.0/contact/users/me (user access token) -> unionId/nick/avatar
      -> server-side session row (token hashed at rest) + httpOnly cookie -> 302 back

The guard protects /api/* except an explicit allowlist; static assets stay
public (they contain no data). Sessions can be revoked by deleting rows.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select

from .config import Settings, get_settings
from .db import AuthSession, SessionLocal, utcnow

COOKIE_NAME = "kg_session"
SESSION_TTL = timedelta(days=7)
STATE_TTL_SECONDS = 600
# Export endpoints have their own mandatory X-API-Key guard.  They must bypass
# the browser-cookie guard so bi_center can pull them headlessly, but no export
# handler is allowed to omit its separate guard.
PUBLIC_API_PATHS = ("/api/health", "/api/auth/", "/api/export/")


def _sign(settings: Settings, payload: str) -> str:
    return hmac.new(settings.auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(settings: Settings, return_url: str) -> str:
    payload = json.dumps({"t": int(time.time()), "r": return_url}, separators=(",", ":"))
    token = payload.encode().hex()
    return f"{token}.{_sign(settings, token)}"


def read_state(settings: Settings, state: str) -> dict:
    token, _, signature = state.partition(".")
    if not token or not hmac.compare_digest(signature, _sign(settings, token)):
        raise ValueError("state_invalid")
    payload = json.loads(bytes.fromhex(token).decode())
    if time.time() - payload.get("t", 0) > STATE_TTL_SECONDS:
        raise ValueError("state_expired")
    return payload


def sanitize_return_url(settings: Settings, raw: str) -> str:
    base = settings.public_base_url.rstrip("/")
    if raw and (raw.startswith(base) or raw.startswith("/")):
        return raw
    return base + "/"


def build_login_url(settings: Settings, return_url: str) -> str:
    params = {
        "client_id": settings.dingtalk_app_key,
        "response_type": "code",
        "scope": "openid corpid",
        "prompt": "consent",
        "state": make_state(settings, sanitize_return_url(settings, return_url)),
        "redirect_uri": f"{settings.public_base_url.rstrip('/')}/api/auth/dingtalk/callback",
    }
    return f"https://login.dingtalk.com/oauth2/auth?{urlencode(params)}"


async def exchange_user(settings: Settings, code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        token_response = await client.post(
            "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
            json={"clientId": settings.dingtalk_app_key, "clientSecret": settings.dingtalk_app_secret,
                  "code": code, "grantType": "authorization_code"},
        )
        if token_response.is_error:
            raise ValueError(f"token_exchange_failed_{token_response.status_code}")
        token_payload = token_response.json()
        corp_id = token_payload.get("corpId", "")
        if settings.dingtalk_corp_id and corp_id and corp_id != settings.dingtalk_corp_id:
            raise ValueError("corp_mismatch")
        user_token = token_payload.get("accessToken", "")
        if not user_token:
            raise ValueError("user_token_missing")
        me = await client.get("https://api.dingtalk.com/v1.0/contact/users/me",
                              headers={"x-acs-dingtalk-access-token": user_token})
        if me.is_error:
            raise ValueError(f"profile_fetch_failed_{me.status_code}")
        profile = me.json()
    union_id = profile.get("unionId", "")
    if not union_id:
        raise ValueError("union_id_missing")
    return {"union_id": union_id, "name": profile.get("nick", ""), "avatar": profile.get("avatarUrl", "")}


def issue_session(user: dict) -> str:
    token = secrets.token_urlsafe(40)
    with SessionLocal() as db:
        db.add(AuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                           union_id=user["union_id"], name=user.get("name", ""), avatar=user.get("avatar", ""),
                           expires_at=utcnow() + SESSION_TTL))
        db.execute(delete(AuthSession).where(AuthSession.expires_at < utcnow()))
        db.commit()
    return token


def resolve_session(token: str) -> dict | None:
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with SessionLocal() as db:
        row = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
        if not row or (row.expires_at and row.expires_at.replace(tzinfo=None) < utcnow().replace(tzinfo=None)):
            return None
        row.last_seen_at = utcnow()
        db.commit()
        return {"union_id": row.union_id, "name": row.name, "avatar": row.avatar}


def revoke_session(token: str) -> None:
    if not token:
        return
    with SessionLocal() as db:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
        db.commit()


def set_cookie(response, token: str, settings: Settings) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
                        samesite="lax", secure=settings.public_base_url.startswith("https"), path="/")


async def guard_middleware(request: Request, call_next):
    """401 for /api/* without a valid session. Static assets stay public."""
    settings = get_settings()
    path = request.url.path
    if settings.auth_enabled and path.startswith("/api/") and not any(path.startswith(p) for p in PUBLIC_API_PATHS):
        user = resolve_session(request.cookies.get(COOKIE_NAME, ""))
        if not user:
            return JSONResponse(status_code=401, content={"detail": {"code": "unauthorized", "message": "请先登录。"}})
        request.state.user = user
    return await call_next(request)


def register_auth_routes(app) -> None:
    @app.get("/api/auth/config")
    def auth_config():
        settings = get_settings()
        return {"auth_enabled": settings.auth_enabled, "corp_id": settings.dingtalk_corp_id}

    @app.get("/api/auth/me")
    def me(request: Request):
        settings = get_settings()
        if not settings.auth_enabled:
            return {"auth_enabled": False, "user": {"union_id": "", "name": settings.default_actor, "avatar": ""}}
        user = resolve_session(request.cookies.get(COOKIE_NAME, ""))
        if not user:
            return JSONResponse(status_code=401, content={"detail": {"code": "unauthorized", "message": "未登录。"}})
        return {"auth_enabled": True, "user": user}

    @app.get("/api/auth/login-url")
    def login_url(return_url: str = ""):
        settings = get_settings()
        if not settings.dingtalk_app_key:
            return JSONResponse(status_code=400, content={"detail": {"code": "app_key_missing", "message": "未配置钉钉应用凭据。"}})
        return {"login_url": build_login_url(settings, return_url)}

    @app.get("/api/auth/dingtalk/callback")
    async def callback(request: Request, authCode: str = "", code: str = "", state: str = ""):
        settings = get_settings()
        target = settings.public_base_url.rstrip("/") + "/"
        try:
            payload = read_state(settings, state)
            target = sanitize_return_url(settings, payload.get("r", ""))
            user = await exchange_user(settings, authCode or code)
        except ValueError as exc:
            return JSONResponse(status_code=401, content={"detail": {"code": str(exc), "message": "钉钉登录失败，请重试。"}})
        token = issue_session(user)
        response = RedirectResponse(url=target, status_code=302)
        set_cookie(response, token, settings)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        revoke_session(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse(content={"status": "ok"})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response
