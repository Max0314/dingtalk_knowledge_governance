"""Bounded process-local measurements: route templates and timings, never parameters."""
from collections import OrderedDict, deque
from contextvars import ContextVar
import math
import functools
import threading
import time

from sqlalchemy import event

_queries = ContextVar("kg_request_queries", default=None)
_samples = OrderedDict()
_refreshes = OrderedDict()
_lock = threading.Lock()


def _before(conn, cursor, statement, parameters, context, executemany):
    context.kg_started_at = time.monotonic()


def _after(conn, cursor, statement, parameters, context, executemany):
    bucket = _queries.get()
    if bucket is not None:
        # One request cannot retain an unbounded query list.
        bucket[0] += (time.monotonic() - context.kg_started_at) * 1000
        bucket[1] += 1


def install(app, engine):
    if not event.contains(engine, "before_cursor_execute", _before):
        event.listen(engine, "before_cursor_execute", _before)
        event.listen(engine, "after_cursor_execute", _after)

    @app.middleware("http")
    async def measure(request, call_next):
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        bucket = [0.0, 0]
        token = _queries.set(bucket)
        started, status, cache = time.monotonic(), 500, ""
        try:
            response = await call_next(request)
            status, cache = response.status_code, response.headers.get("x-kg-cache", "")
            response.headers["Server-Timing"] = f"app;dur={(time.monotonic()-started)*1000:.1f}, db;dur={bucket[0]:.1f}"
            return response
        finally:
            elapsed = (time.monotonic() - started) * 1000
            route = getattr(request.scope.get("route"), "path", "")
            if route and route != "/api/v1/diagnostics/performance":
                key = request.method + " " + route
                with _lock:
                    rows = _samples.setdefault(key, deque(maxlen=256))
                    rows.append((elapsed, bucket[0], bucket[1], cache, status >= 400))
                    _samples.move_to_end(key)
                    while len(_samples) > 64:
                        _samples.popitem(last=False)
            _queries.reset(token)


def snapshot():
    with _lock:
        values = [(key, list(rows)) for key, rows in _samples.items()]
    items = []
    for key, rows in values:
        times = sorted(r[0] for r in rows)
        items.append({"route": key, "samples": len(rows),
                      "p95_ms": round(times[math.ceil(len(times)*.95)-1], 1),
                      "db_avg_ms": round(sum(r[1] for r in rows)/len(rows), 1),
                      "db_queries_avg": round(sum(r[2] for r in rows)/len(rows), 1),
                      "cache_hits": sum(r[3] == "hit" for r in rows),
                      "cache_stale": sum(r[3] == "stale" for r in rows),
                      "errors": sum(r[4] for r in rows)})
    with _lock:
        refreshes = [(key, list(rows)) for key, rows in _refreshes.items()]
    refresh_rows = []
    for key, rows in refreshes:
        times = sorted(r[0] for r in rows)
        refresh_rows.append({"endpoint": key, "samples": len(rows), "p95_ms": round(times[math.ceil(len(times)*.95)-1], 1),
                             "db_avg_ms": round(sum(r[1] for r in rows)/len(rows), 1), "errors": sum(r[2] for r in rows)})
    return {"items": sorted(items, key=lambda r: -r["p95_ms"]), "refreshes": refresh_rows,
            "route_limit": 64, "samples_per_route": 256}


def measure_refresh(fn):
    @functools.wraps(fn)
    def measured(key, name, parameters):
        bucket, failed, started = [0.0, 0], True, time.monotonic()
        token = _queries.set(bucket)
        try:
            result = fn(key, name, parameters)
            failed = False
            return result
        finally:
            with _lock:
                _refreshes.setdefault(name, deque(maxlen=64)).append(((time.monotonic()-started)*1000, bucket[0], failed))
                _refreshes.move_to_end(name)
                while len(_refreshes) > 32:
                    _refreshes.popitem(last=False)
            _queries.reset(token)
    return measured
