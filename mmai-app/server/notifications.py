"""Bounded outbox dispatch independent of slow hourly matching.

One consumer per outbox keeps a slow sender from blocking the other queues.
Normal configured delivery is checked every second, including after restart.
No new events are produced, held rows are never released, and ambiguous sending
rows require manual investigation rather than automatic resend.
"""
from __future__ import annotations

import logging
import os
import threading

from . import alerts, homebot_receipts, receipts
from .db import assert_not_guarded

log = logging.getLogger(__name__)
_threads = {}
_start_lock = threading.Lock()
_stop = threading.Event()
INTERVAL_S = 1
LIMIT = 25


def enabled():
    return os.environ.get("MMAI_NOTIFICATIONS", os.environ.get("MMAI_SCHEDULER", "1")) == "1"


def queues():
    return {"matebot_receipts": (receipts.mail_mode, receipts.deliver_pending),
            "homebot_receipts": (receipts.mail_mode, homebot_receipts.deliver_pending),
            "internal_alerts": (alerts.alert_mode, alerts.deliver_pending)}


def dispatch_once(c, name):
    mode, deliver = queues()[name]
    if not enabled() or mode() == "off":
        return None  # no database reads, threads or transport construction while disabled
    assert_not_guarded(c, "notification dispatch")
    return deliver(c, limit=LIMIT)


def _loop(c, name):
    while not _stop.is_set():
        try:
            dispatch_once(c, name)
        except Exception as ex:
            log.warning("notification pass failed queue=%s error=%s", name, type(ex).__name__)
        _stop.wait(INTERVAL_S)


def start(c):
    if not enabled():
        return []
    with _start_lock:
        for name, (mode, _) in queues().items():
            if mode() == "off" or (name in _threads and _threads[name].is_alive()):
                continue
            worker = threading.Thread(target=_loop, args=(c, name), daemon=True, name=f"mmai-{name}")
            _threads[name] = worker
            worker.start()
        return list(_threads.values())
