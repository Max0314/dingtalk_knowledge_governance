"""Host-side staging-only restart, cold-cache, DB outage and rollback drills."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--rollback-image", required=True)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    if not args.project.startswith("kg_validation_") or not str(directory).startswith("/home/max/apps/kg-validation-"):
        raise ValueError("staging_only")
    environment = dict(os.environ, KG_PUBLISH_PORT="39028", KG_ROLLBACK_IMAGE=args.rollback_image)
    base = ["docker", "compose", "-p", args.project, "-f", "docker-compose.yml"]
    def command(parts, extra=None, input=None):
        cli = base + (["-f", extra] if extra else []) + parts
        result = subprocess.run(cli, cwd=directory, env=environment, input=input,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError("compose_drill_failed")  # no secrets/SQL/errors printed
        return result.stdout
    def probe(path="/api/ready"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:39028" + path, timeout=2) as response:
                return response.status == 200
        except Exception:
            return False
    def wait(path="/api/ready"):
        start = time.monotonic()
        while time.monotonic()-start < 75:
            if probe(path):
                return round(time.monotonic()-start, 2)
            time.sleep(.25)
        raise RuntimeError("readiness_timeout")
    source = (directory / "scripts/ops_resilience.py").read_text()
    def check():
        result = command(["exec", "-T", "api", "python", "-", "benchmark", "--seconds", "5", "--rps", "10", "--concurrency", "10"], input=source)
        return json.loads(result.strip().splitlines()[-1])
    reports = {}
    try:
        started = time.monotonic()
        command(["restart", "api"])
        wait()
        reports["restart"] = {"ready_seconds": round(time.monotonic()-started,2), "test": check()}
        command(["stop", "api"])
        wipe = "from app.db import SessionLocal, ReadCacheEntry\nfrom sqlalchemy import delete\nwith SessionLocal() as db:\n db.execute(delete(ReadCacheEntry)); db.commit()\n"
        command(["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "api", "-"], input=wipe)
        started = time.monotonic()
        command(["up", "-d", "--no-deps", "--no-build", "api"])
        wait()
        reports["cache_loss"] = {"ready_seconds": round(time.monotonic()-started,2), "test": check()}
        command(["up", "-d", "--no-deps", "--no-build", "api"], extra="ops/compose.db-outage.yml")
        time.sleep(3)
        reports["database_outage"] = {"readiness_rejected": not probe()}
        started = time.monotonic()
        command(["up", "-d", "--no-deps", "--no-build", "api"])
        wait()
        reports["database_outage"]["restored_seconds"] = round(time.monotonic()-started,2)
        reports["database_outage"]["test"] = check()
        started = time.monotonic()
        command(["up", "-d", "--no-deps", "--no-build", "api"], extra="ops/compose.rollback.yml")
        wait("/api/health")
        reports["rollback"] = {"ready_seconds": round(time.monotonic()-started,2), "test": check()}
    finally:
        command(["up", "-d", "--no-deps", "--no-build", "api"])
        wait()
    reports["forward_again"] = check()
    print(json.dumps(reports))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"failed": type(error).__name__}))
        raise SystemExit(1)
