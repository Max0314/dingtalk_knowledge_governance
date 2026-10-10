"""Operator-only bounded load tests and encrypted, isolated restore drills.

Never calls DingTalk/BI/model APIs. No credentials or row contents are printed.
Run in the API image; backup output must be copied out of /tmp immediately.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import sys
import time
import uuid
import gzip
from datetime import date, datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Column, Date, DateTime, LargeBinary, MetaData, Table, delete, func, select, text
from app.db import AuthSession, SessionLocal, engine, utcnow

MAGIC = b"KGDBENC1"
EXCLUDED = {"auth_sessions", "read_cache_entries"}  # sessions must re-login on restore


def canonical(row):
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def metadata():
    value = MetaData()
    value.reflect(engine)
    # Only known service tables, not temporary drill tables/other services.
    from app.db import Base
    return {name: value.tables[name] for name in sorted(Base.metadata.tables)
            if name in value.tables and name not in EXCLUDED}


def snapshot():
    tables = metadata()
    with engine.connect() as connection:
        counts = {name: connection.scalar(select(func.count()).select_from(table)) for name, table in tables.items()}
        usage = tables.get("dingtalk_api_usage")
        attempts = connection.scalar(select(func.sum(usage.c.calls))) or 0 if usage is not None else 0
    return {"tables": len(counts), "counts": counts, "dingtalk_attempt_counter_sum": attempts}


def backup(path):
    target = Path(path).resolve()
    if not target.parent.is_dir() or target.exists() or target.with_suffix(".key").exists():
        raise ValueError("backup_target_invalid")
    key = secrets.token_bytes(32)
    key_path = target.with_suffix(".key")
    with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(key)
    cipher, prefix, sequence = AESGCM(key), secrets.token_bytes(4), 0
    tables = metadata()
    counts, digests, buffer = {}, {}, bytearray()
    started = time.monotonic()
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(MAGIC + prefix)
        def flush():
            nonlocal sequence
            if not buffer:
                return
            nonce = prefix + struct.pack(">Q", sequence)
            encrypted = cipher.encrypt(nonce, gzip.compress(bytes(buffer)), MAGIC)
            handle.write(struct.pack(">I", len(encrypted)) + encrypted)
            sequence += 1
            buffer.clear()
        # MySQL default repeatable-read: one consistent snapshot, no table locks.
        with engine.connect() as connection, connection.begin():
            for name, table in tables.items():
                counts[name], digest = 0, hashlib.sha256()
                for row in connection.execution_options(stream_results=True).execute(
                        select(table).order_by(*table.primary_key.columns)).mappings():
                    values = dict(row)
                    for column in table.columns:
                        if isinstance(values.get(column.name), bytes):
                            values[column.name] = base64.b64encode(values[column.name]).decode()
                    line = canonical({"table": name, "row": values}) + b"\n"
                    digest.update(canonical(values) + b"\n")
                    buffer.extend(line)
                    counts[name] += 1
                    if len(buffer) >= 1024 * 1024:
                        flush()
                digests[name] = digest.hexdigest()
            buffer.extend(canonical({"manifest": {"counts": counts, "digests": digests}}) + b"\n")
            flush()
    return {"backup": str(target), "key_file": str(key_path), "tables": len(tables),
            "rows": sum(counts.values()), "bytes": target.stat().st_size,
            "seconds": round(time.monotonic() - started, 2), "excluded": sorted(EXCLUDED)}


def records(path, key_path):
    cipher = AESGCM(Path(key_path).read_bytes())
    with Path(path).open("rb") as handle:
        if handle.read(8) != MAGIC:
            raise ValueError("backup_format_invalid")
        prefix, sequence = handle.read(4), 0
        while size := handle.read(4):
            length = struct.unpack(">I", size)[0]
            if length > 16 * 1024 * 1024:
                raise ValueError("backup_chunk_invalid")
            payload = cipher.decrypt(prefix + struct.pack(">Q", sequence), handle.read(length), MAGIC)
            for line in gzip.decompress(payload).splitlines():
                yield json.loads(line)
            sequence += 1


def restore_drill(path, key_path):
    # Dedicated prefix, no writes to canonical tables; FK validity checked
    # separately. Finally only these exact freshly-created tables are removed.
    prefix = "kg_restore_" + uuid.uuid4().hex[:10] + "_"
    sources, target_meta, targets = metadata(), MetaData(), {}
    counts, digests, batches = {}, {}, {}
    started, manifest, foreign_errors = time.monotonic(), None, 0
    try:
        for name, source in sources.items():
            targets[name] = Table(prefix + name, target_meta, *[
                Column(col.name, col.type, primary_key=col.primary_key, nullable=col.nullable)
                for col in source.columns])
            counts[name], digests[name], batches[name] = 0, hashlib.sha256(), []
        target_meta.create_all(engine)
        with engine.begin() as connection:
            for event in records(path, key_path):
                if "manifest" in event:
                    manifest = event["manifest"]
                    continue
                name, row = event["table"], event["row"]
                if name not in targets:
                    raise ValueError("unknown_backup_table")
                for col in targets[name].columns:
                    if row.get(col.name) is not None:
                        if isinstance(col.type, DateTime):
                            row[col.name] = datetime.fromisoformat(row[col.name])
                        elif isinstance(col.type, Date):
                            row[col.name] = date.fromisoformat(row[col.name])
                        elif isinstance(col.type, LargeBinary):
                            row[col.name] = base64.b64decode(row[col.name])
                batches[name].append(row)
                if len(batches[name]) >= 500:
                    connection.execute(targets[name].insert(), batches[name])
                    batches[name].clear()
            for name, rows in batches.items():
                if rows:
                    connection.execute(targets[name].insert(), rows)
        if not manifest:
            raise ValueError("missing_manifest")
        with engine.connect() as connection:
            for name, table in targets.items():
                for row in connection.execution_options(stream_results=True).execute(
                        select(table).order_by(*table.primary_key.columns)).mappings():
                    values = dict(row)
                    for col in table.columns:
                        if isinstance(values.get(col.name), bytes):
                            values[col.name] = base64.b64encode(values[col.name]).decode()
                    digests[name].update(canonical(values) + b"\n")
                    counts[name] += 1
                if counts[name] != manifest["counts"].get(name, 0) or (name in manifest["digests"] and digests[name].hexdigest() != manifest["digests"][name]):
                    raise ValueError("restore_verification_failed")
            for name, source in sources.items():
                for fk in source.foreign_keys:
                    other = fk.column.table.name
                    if other not in targets:
                        continue
                    child, parent = targets[name], targets[other]
                    col, ref = child.c[fk.parent.name], parent.c[fk.column.name]
                    foreign_errors += connection.scalar(select(func.count()).select_from(
                        child.outerjoin(parent, col == ref)).where(col.is_not(None), ref.is_(None))) or 0
        return {"verified": True, "tables": len(targets), "rows": sum(counts.values()),
                "column_sha256_match": True, "legacy_orphan_references": foreign_errors,
                "seconds": round(time.monotonic() - started, 2), "production_tables_overwritten": 0}
    finally:
        # Only exact tables allocated by this invocation in this database.
        if not all(table.name.startswith(prefix) for table in targets.values()):
            raise ValueError("invalid_drill_cleanup_scope")
        target_meta.drop_all(engine)


ROUTES = ["/api/v1/dashboard/overview", "/api/v1/metrics/monthly-increments",
          "/api/v1/metrics/increments/tree", "/api/v1/metrics/increments/tree?year=2026",
          "/api/v1/metrics/increments/tree?month=2026-08",
          "/api/v1/metrics/increments/tree?scope=rd_system&year=2026",
          "/api/v1/metrics/org?year=2026", "/api/v1/files?limit=50",
          "/api/v1/workspaces?limit=50", "/api/v1/reviews?limit=50",
          "/api/v1/filters/workspaces", "/api/v1/filters/departments"]


def percentile(values, q):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))], 2) if ordered else None


async def benchmark(base, seconds, rps, concurrency):
    if not base.startswith("http://127.0.0.1:"):
        raise ValueError("benchmark_must_target_local_app")
    token, identity = secrets.token_urlsafe(40), "synthetic-load-" + uuid.uuid4().hex
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with SessionLocal() as db:
        db.add(AuthSession(token_hash=token_hash, union_id=identity, name="Load test",
                           expires_at=utcnow() + timedelta(minutes=10)))
        db.commit()
    elapsed, statuses, per_route, caches, cold = [], {}, {}, {}, {}
    before = snapshot()
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(base_url=base, timeout=35, cookies={"kg_session": token},
                limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)) as client:
            for route in ROUTES:
                initial = time.monotonic()
                result = await client.get(route)
                cold[route] = {"ms": round((time.monotonic()-initial)*1000, 2), "status": result.status_code}
                if result.status_code != 200:
                    raise ValueError("warmup_failed")
            barrier = asyncio.Semaphore(concurrency)
            async def request(index, due):
                await asyncio.sleep(max(0, due - time.monotonic()))
                async with barrier:
                    route = ROUTES[index % len(ROUTES)]
                    initial = time.monotonic()
                    try:
                        result = await client.get(route)
                        code = str(result.status_code)
                        caches[result.headers.get("x-kg-cache", "none")] = caches.get(result.headers.get("x-kg-cache", "none"), 0)+1
                    except httpx.HTTPError:
                        code = "transport_error"
                    ms = (time.monotonic()-initial)*1000
                    elapsed.append(ms); per_route.setdefault(route, []).append(ms)
                    statuses[code] = statuses.get(code, 0) + 1
            zero = time.monotonic()
            await asyncio.gather(*(request(i, zero+i/rps) for i in range(seconds*rps)))
    finally:
        with SessionLocal() as db:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash)); db.commit()
    after = snapshot()
    return {"seconds": round(time.monotonic()-started, 2), "requests": len(elapsed),
            "target_rps": rps, "concurrency": concurrency, "status": statuses,
            "p50_ms": percentile(elapsed,.5), "p95_ms": percentile(elapsed,.95),
            "p99_ms": percentile(elapsed,.99), "max_ms": round(max(elapsed),2),
            "cache": caches, "cold": cold,
            "routes": {route: {"p95_ms": percentile(values,.95), "requests": len(values)} for route,values in per_route.items()},
            "canonical_counts_unchanged": before == after,
            "dingtalk_meter_unchanged": before["dingtalk_attempt_counter_sum"] == after["dingtalk_attempt_counter_sum"],
            "dingtalk_attempt_delta": after["dingtalk_attempt_counter_sum"] - before["dingtalk_attempt_counter_sum"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot", "backup", "restore-drill", "benchmark"])
    parser.add_argument("--path", default="/tmp/kg-release.kgenc")
    parser.add_argument("--key", default="/tmp/kg-release.key")
    parser.add_argument("--base", default="http://127.0.0.1:39021")
    parser.add_argument("--seconds", type=int, choices=range(5,61), default=30)
    parser.add_argument("--rps", type=int, choices=range(1,101), default=40)
    parser.add_argument("--concurrency", type=int, choices=range(1,51), default=20)
    args = parser.parse_args()
    if args.action == "snapshot":
        result = snapshot()
    elif args.action == "backup":
        result = backup(args.path)
    elif args.action == "restore-drill":
        result = restore_drill(args.path, args.key)
    else:
        result = asyncio.run(benchmark(args.base, args.seconds, args.rps, args.concurrency))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"failed": type(error).__name__}))
        sys.exit(1)
