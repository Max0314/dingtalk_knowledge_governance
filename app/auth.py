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
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from .config import Settings, get_settings
from .db import AuthSession, SessionLocal, utcnow

COOKIE_NAME = "kg_session"
SESSION_TTL = timedelta(days=7)
STATE_TTL_SECONDS = 600
# The export namespace is handled explicitly at the top of the middleware so
# it can bypass browser cookies without entering this public-session allowlist.
PUBLIC_API_PATHS = ("/api/health", "/api/ready", "/api/auth/")


def _sign(settings: Settings, payload: str) -> str:
    return hmac.new(settings.auth_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(settings: Settings, return_url: str) -> str:
    payload = json.dumps({"t": int(time.time()), "r": return_url}, separators=(",", ":"))
    token = payload.encode().hex()
    return f"{token}.{_sign(settings, token)}"


def read_state(settings: Settings, state: str) -> dict:
    if len(state) > 4096:
        raise ValueError("state_invalid")
    token, _, signature = state.partition(".")
    if not token or not hmac.compare_digest(signature, _sign(settings, token)):
        raise ValueError("state_invalid")
    try:
        payload = json.loads(bytes.fromhex(token).decode())
    except (ValueError, UnicodeError) as exc:
        raise ValueError("state_invalid") from exc
    if not isinstance(payload, dict) or type(payload.get("t")) is not int:
        raise ValueError("state_invalid")
    now = time.time()
    if not now - STATE_TTL_SECONDS <= payload["t"] <= now + 30:
        raise ValueError("state_expired")
    return payload


def sanitize_return_url(settings: Settings, raw: str) -> str:
    base = settings.public_base_url.rstrip("/")
    fallback = base + "/"
    if not isinstance(raw, str) or not raw or raw.startswith("//") or "\\" in raw:
        return fallback
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return fallback
    try:
        expected = urlsplit(base)
        target = urlsplit(urljoin(fallback, raw))
        # Only workspace entry documents can be returned to. Comparing an
        # origin/path prefix alone also admits sibling apps and lookalike hosts.
        allowed_paths = {expected.path.rstrip("/") + "/",
                         expected.path.rstrip("/") + "/index.html"}
        if (target.scheme, target.netloc) != (expected.scheme, expected.netloc):
            return fallback
        if target.path not in allowed_paths or target.username or target.password:
            return fallback
    except ValueError:
        return fallback
    return target.geturl()


def login_redirect(settings: Settings, return_url: str = "", error: str = "") -> RedirectResponse:
    params = {"return_url": sanitize_return_url(settings, return_url)}
    if error:
        params["error"] = error
    response = RedirectResponse(
        f"{settings.public_base_url.rstrip('/')}/login.html?{urlencode(params)}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


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
        if not isinstance(token_payload, dict):
            raise ValueError("invalid_login_response")
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
        if not isinstance(profile, dict):
            raise ValueError("invalid_login_response")
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
        user = {"union_id": row.union_id, "name": row.name, "avatar": row.avatar}
        # Avoid a transaction write (and expired-ORM re-read) for every GET.
        if not row.last_seen_at or utcnow().replace(tzinfo=None) - row.last_seen_at.replace(tzinfo=None) >= timedelta(minutes=5):
            row.last_seen_at = utcnow()
            db.commit()
        return user


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
    if path.startswith("/api/export/"):
        # Fail closed for the whole namespace. Route-local checks remain as
        # defence in depth, but a future export handler cannot accidentally
        # become public merely because its author forgot that dependency.
        from . import bi_export
        try:
            bi_export.authorize(request, settings)
        except bi_export.ExportApiError as error:
            response = JSONResponse(status_code=error.status_code,
                                    content={"ok": False, "error": error.code})
            response.headers["Cache-Control"] = "no-store"
            return response
        response = await call_next(request)
        # Employee/workspace facts are authenticated but still sensitive;
        # prevent browsers and intermediary proxies from retaining them.
        response.headers["Cache-Control"] = "no-store"
        return response
    workspace_entry = path in ("/", "/index.html")
    protected_api = path.startswith("/api/") and not any(path.startswith(p) for p in PUBLIC_API_PATHS)
    if settings.auth_enabled and (workspace_entry or protected_api):
        try:
            # Session lookup is synchronous SQL: never block the ASGI event loop.
            user = await run_in_threadpool(resolve_session, request.cookies.get(COOKIE_NAME, ""))
        except SQLAlchemyError:
            if workspace_entry:
                return login_redirect(settings, error="session_store_unavailable")
            return JSONResponse(status_code=503, content={"detail": {
                "code": "session_store_unavailable", "message": "登录服务暂时不可用，请稍后重试。"}})
        if not user:
            if workspace_entry:
                return login_redirect(settings, str(request.url))
            return JSONResponse(status_code=401, content={"detail": {"code": "unauthorized", "message": "请先登录。"}})
        request.state.user = user
    response = await call_next(request)
    if path.startswith("/api/auth/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
        try:
            user = resolve_session(request.cookies.get(COOKIE_NAME, ""))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"detail": {
                "code": "session_store_unavailable", "message": "登录服务暂时不可用，请稍后重试。"}})
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
            if not (authCode or code):
                raise ValueError("code_missing")
            user = await exchange_user(settings, authCode or code)
        except ValueError as exc:
            reason = str(exc)
            if reason not in {"state_invalid", "state_expired", "code_missing", "corp_mismatch"}:
                reason = "authorization_failed"
            return login_redirect(settings, target, reason)
        except httpx.HTTPError:
            return login_redirect(settings, target, "authorization_unavailable")
        try:
            token = await run_in_threadpool(issue_session, user)
        except SQLAlchemyError:
            return login_redirect(settings, target, "session_store_unavailable")
        response = RedirectResponse(url=target, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        set_cookie(response, token, settings)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        revoke_session(request.cookies.get(COOKIE_NAME, ""))
        response = JSONResponse(content={"status": "ok"})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response
