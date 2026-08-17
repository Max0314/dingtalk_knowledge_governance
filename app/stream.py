"""DingTalk Stream consumer: receives pushed events over an outbound
connection and lands them in the stream_events table for inspection and
processing. No public callback URL, no polling quota.

Runs as a daemon thread inside the worker when KG_STREAM_ENABLED=true. The
import and the connection are both guarded: a missing SDK or a gateway outage
degrades to a log line, never a crashed worker.
"""
from __future__ import annotations

import json
import logging
import threading

from .config import Settings
from .db import SessionLocal, StreamEvent

logger = logging.getLogger("kg.stream")
MAX_PAYLOAD_CHARS = 20000


def _store_event(event_type: str, biz_id: str, payload: dict) -> None:
    try:
        with SessionLocal() as db:
            db.add(StreamEvent(event_type=event_type[:128], biz_id=str(biz_id)[:128],
                               payload=json.dumps(payload, ensure_ascii=False)[:MAX_PAYLOAD_CHARS]))
            db.commit()
    except Exception:
        logger.exception("failed to store stream event %s", event_type)


def _run_client(settings: Settings) -> None:
    try:
        import dingtalk_stream
        from dingtalk_stream import AckMessage
    except ImportError:
        logger.warning("dingtalk-stream SDK not installed; stream consumer disabled")
        return
    # The SDK sets its client logger to INFO while importing. Reset it after
    # import so the temporary WebSocket connection ticket never enters logs.
    logging.getLogger("dingtalk_stream.client").setLevel(logging.WARNING)

    class AllEventsHandler(dingtalk_stream.EventHandler):
        async def process(self, event: "dingtalk_stream.EventMessage"):
            header = event.headers
            data = event.data if isinstance(event.data, dict) else {"raw": str(event.data)[:2000]}
            _store_event(getattr(header, "event_type", "") or "", getattr(header, "event_id", "") or "", data)
            return AckMessage.STATUS_OK, "OK"

    credential = dingtalk_stream.Credential(settings.dingtalk_app_key, settings.dingtalk_app_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_all_event_handler(AllEventsHandler())
    logger.info("stream consumer connecting")
    client.start_forever()


def start_stream_consumer(settings: Settings) -> threading.Thread | None:
    if not settings.stream_enabled or not settings.dingtalk_app_key:
        return None
    thread = threading.Thread(target=_run_client, args=(settings,), name="dingtalk-stream", daemon=True)
    thread.start()
    return thread
