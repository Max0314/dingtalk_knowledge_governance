"""Bounded, single-flight cache for authenticated, local metadata reads only.

MySQL keeps the last response across API restarts. A database lease coalesces
refreshes across processes; two threads bound expensive aggregation work. No
request, cookie, session, document body or external client is retained here.
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.params import Param
from fastapi.responses import Response
from fastapi.routing import APIRoute
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .db import ReadCacheEntry, SessionLocal, utcnow

# Explicit allowlist: adding a route never implicitly enables its caching.
ENDPOINTS = {
    "dashboard", "metrics_monthly", "metrics_coverage", "metrics_increments_tree",
    "metrics_workspace_months", "uploader_months_api", "uploaders_api",
    "uploader_detail_api", "uploader_breakdown_api", "org_rollup_api",
    "departments_api", "baseline_folders", "baseline_files", "files_unified",
    "reviews_list", "workspace_options", "department_options", "workspaces", "documents",
}
MAX_KEYS = 256
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 2 * 1024 * 1024
LEASE_SECONDS = 120
_memory: OrderedDict = OrderedDict()
_bytes = 0
_lock = threading.RLock()
_flights = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kg-read-cache")
_registry = {}
_stop = threading.Event()
_maintenance = None


def enabled():
    settings = get_settings()
    return settings.read_cache_enabled and not settings.demo_mode


def _age(generated):
    return max(0.0, (utcnow().replace(tzinfo=None) - generated.replace(tzinfo=None)).total_seconds())


def _key(name, parameters):
    raw = json.dumps(["metadata-v1", name, parameters], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _remember(key, payload, generated):
    global _bytes
    body = json.dumps(jsonable_encoder(payload), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()
    if len(body) > MAX_ENTRY_BYTES:
        raise HTTPException(503, "查询结果过大，请缩小筛选范围。")
    with _lock:
        old = _memory.pop(key, None)
        if old:
            _bytes -= len(old[0])
        _memory[key] = (body, generated)
        _bytes += len(body)
        while len(_memory) > MAX_KEYS or _bytes > MAX_BYTES:
            _, removed = _memory.popitem(last=False)
            _bytes -= len(removed[0])
    return body, generated


def _load(key):
    with _lock:
        entry = _memory.get(key)
        if entry:
            _memory.move_to_end(key)
            return entry
    with SessionLocal() as db:
        row = db.get(ReadCacheEntry, key)
        if row and row.payload is not None and row.generated_at:
            return _remember(key, row.payload, row.generated_at)
    return None


def _refresh(key, name, parameters):
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + 20
    while True:
        now = utcnow()
        with SessionLocal() as db:
            row = db.get(ReadCacheEntry, key)
            if row is None:
                db.add(ReadCacheEntry(cache_key=key, endpoint=name, parameters=parameters))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
            got = db.execute(update(ReadCacheEntry).where(
                ReadCacheEntry.cache_key == key,
                or_(ReadCacheEntry.lease_until.is_(None), ReadCacheEntry.lease_until < now)
            ).values(lease_owner=owner, lease_until=now + timedelta(seconds=LEASE_SECONDS)))
            db.commit()
            if got.rowcount:
                break
            db.expire_all()
            row = db.get(ReadCacheEntry, key)
            if row and row.generated_at and row.payload is not None and _age(row.generated_at) < get_settings().read_cache_ttl_seconds:
                return _remember(key, row.payload, row.generated_at)
        if time.monotonic() >= deadline:
            raise HTTPException(503, "数据正在准备，请稍后重试。", headers={"Retry-After": "2"})
        time.sleep(.2)
    try:
        fn = _registry[name][0]
        with SessionLocal() as db:
            payload = jsonable_encoder(fn(**parameters, db=db))
        generated = utcnow()
        # Validate and enforce response size BEFORE storing it or publishing.
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > MAX_ENTRY_BYTES:
            raise HTTPException(503, "查询结果过大，请缩小筛选范围。")
        with SessionLocal() as db:
            got = db.execute(update(ReadCacheEntry).where(
                ReadCacheEntry.cache_key == key, ReadCacheEntry.lease_owner == owner
            ).values(payload=payload, generated_at=generated, lease_owner="", lease_until=None))
            db.commit()
            if not got.rowcount:
                raise HTTPException(503, "刷新租约已过期，请重试。")
        return _remember(key, payload, generated)
    finally:
        with SessionLocal() as db:
            db.execute(update(ReadCacheEntry).where(ReadCacheEntry.cache_key == key,
                       ReadCacheEntry.lease_owner == owner).values(lease_owner="", lease_until=None))
            db.commit()


def _launch(key, name, parameters):
    with _lock:
        flight = _flights.get(key)
        if flight:
            return flight
        # Bound waiting work as well as threads; arbitrary filter combinations
        # cannot accumulate an unbounded executor queue.
        if len(_flights) >= 32:
            raise HTTPException(503, "查询繁忙，请稍后重试。", headers={"Retry-After": "2"})
        flight = _executor.submit(_refresh, key, name, parameters)
        _flights[key] = flight
        def finished(_):
            with _lock:
                _flights.pop(key, None)
        flight.add_done_callback(finished)
        return flight


def read(name, parameters):
    settings = get_settings()
    key = _key(name, parameters)
    entry = _load(key)
    age = _age(entry[1]) if entry else float("inf")
    status = "hit"
    if age >= min(settings.read_cache_ttl_seconds, settings.read_cache_max_age_seconds):
        future = _launch(key, name, parameters)
        if entry and age < settings.read_cache_max_age_seconds:
            status = "stale"
        else:
            try:
                entry = future.result(timeout=25)
            except TimeoutError:
                raise HTTPException(503, "数据正在准备，请稍后重试。", headers={"Retry-After": "2"}) from None
            status = "miss"
    return Response(entry[0], media_type="application/json", headers={
        "Cache-Control": "private, no-store", "X-KG-Cache": status,
        "X-KG-Data-Age": str(int(_age(entry[1]))),
        "X-KG-Generated-At": entry[1].isoformat(),
    })


def install(app):
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.endpoint.__name__ not in ENDPOINTS:
            continue
        fn = route.endpoint
        defaults = {}
        for key, param in inspect.signature(fn).parameters.items():
            if key != "db" and param.default is not inspect.Parameter.empty:
                defaults[key] = param.default.default if isinstance(param.default, Param) else param.default
        _registry[fn.__name__] = (fn, defaults)
        def make_wrapper(original, values):
            @functools.wraps(original)
            def wrapper(**kwargs):
                if not enabled():
                    return original(**kwargs)
                parameters = {**values, **{k: v for k, v in kwargs.items() if k != "db"}}
                return read(original.__name__, parameters)
            return wrapper
        # Dependency and auth processing still happens before this call.
        route.dependant.call = make_wrapper(fn, defaults)


def _maintain():
    # Core screens prewarm after startup, independently of the first viewer.
    core = ["dashboard", "metrics_monthly", "metrics_increments_tree",
            "uploader_months_api", "files_unified", "workspaces", "reviews_list"]
    while not _stop.is_set():
        if enabled():
            try:
                targets = {(name, _key(name, _registry[name][1])): _registry[name][1]
                           for name in core if name in _registry}
                with SessionLocal() as db:
                    rows = db.scalars(select(ReadCacheEntry).where(
                        ReadCacheEntry.generated_at > utcnow() - timedelta(hours=1)
                    ).order_by(ReadCacheEntry.generated_at.desc()).limit(MAX_KEYS)).all()
                    targets.update({(row.endpoint, row.cache_key): row.parameters for row in rows
                                    if row.endpoint in _registry})
                    db.execute(delete(ReadCacheEntry).where(
                        ReadCacheEntry.lease_until.is_(None),
                        or_(ReadCacheEntry.generated_at < utcnow() - timedelta(days=1),
                            ReadCacheEntry.generated_at.is_(None))))
                    db.commit()
                for (name, key), params in targets.items():
                    if _stop.is_set():
                        break
                    entry = _load(key)
                    if not entry or _age(entry[1]) >= get_settings().read_cache_ttl_seconds:
                        try:
                            _launch(key, name, params).result(timeout=25)
                        except Exception:
                            pass  # Retry next cycle; never log identities/filters/errors.
            except Exception:
                pass
        _stop.wait(60)


def start():
    global _maintenance
    if enabled() and (_maintenance is None or not _maintenance.is_alive()):
        _stop.clear()
        _maintenance = threading.Thread(target=_maintain, daemon=True, name="kg-cache-maintenance")
        _maintenance.start()


def stop():
    _stop.set()
    if _maintenance:
        _maintenance.join(timeout=2)


def clear_memory():
    global _bytes
    with _lock:
        _memory.clear()
        _bytes = 0
