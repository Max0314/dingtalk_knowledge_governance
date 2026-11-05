"""Host-shared request pacing. No SQL, credentials or request data in its state."""
import asyncio
import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def shared_lock(settings, name):
    root = Path(settings.dingtalk_rate_dir or (Path(tempfile.gettempdir()) / 'kg-dingtalk-rate'))
    root.mkdir(mode=0o770, parents=True, exist_ok=True)
    key = hashlib.sha256(settings.dingtalk_app_key.encode()).hexdigest()[:24]
    fd = os.open(root / (key + '-' + name), os.O_RDWR | os.O_CREAT, 0o660)
    with os.fdopen(fd, 'r+b') as handle:
        locked = False
        try:
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError as exc:
                    if exc.errno not in (13, 36):
                        raise
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    pass
            yield handle if locked else None
        finally:
            if locked:
                if os.name == 'nt':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def _try_slot(settings):
    with shared_lock(settings, 'qps') as handle:
        if handle is None:
            return 0.01
        now = time.monotonic_ns()
        interval = 1_000_000_000 / settings.dingtalk_qps
        handle.seek(1)  # byte zero is reserved for the Windows lock
        raw = handle.read(64)
        last = int(raw) if raw else 0
        delay = (last + interval - now) / 1_000_000_000
        if 0 < delay <= 1 and last <= now:
            return delay
        handle.seek(0)
        handle.write(b'0' + str(now).encode())
        handle.truncate()
        handle.flush()
        return 0


async def acquire(settings):
    """Reserve at send time, never future slots; cancellation cannot strand a queue."""
    try:
        while True:
            delay = _try_slot(settings)
            if not delay:
                return
            await asyncio.sleep(delay)
    except (OSError, ValueError) as exc:
        from .integrations import IntegrationError
        raise IntegrationError('dingtalk_rate_unavailable', '接口限速状态暂不可用，请稍后重试。', 503) from exc
