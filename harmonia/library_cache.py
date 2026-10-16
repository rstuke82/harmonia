"""Saved account-scoped Plex reads, with opt-in fresh sync verification."""

import contextvars
import hashlib
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

_policy = contextvars.ContextVar("library_cache_policy", default=None)
_lock = threading.Lock()
_entries = OrderedDict()
_loading = {}
_epoch = 0


@contextmanager
def reuse(minutes=15, refresh=False):
    token = _policy.set({"seconds": minutes * 60, "refresh": refresh, "seen": set()})
    try:
        yield
    finally:
        _policy.reset(token)


def load(client, fetch):
    policy = _policy.get()
    # Ordinary reads consume the last completed account-scoped scan, without
    # an age expiry or a network fallback. Only explicit refresh work fetches.
    if policy is None or not policy["refresh"]:
        from .api import job_store
        from .inventory import snapshot, identity
        from fastapi import HTTPException

        saved = snapshot(job_store().repository, "plex", identity(client))
        if not saved or not saved.get("checked_at"):
            raise HTTPException(
                409,
                "No saved Plex inventory is available for this account. Run Scan Plex Library first.",
            )
        return [dict(t) for t in saved.get("rows", [])]
    identity = hashlib.sha256(
        repr((client.base_url, client.music_library_key, sorted(client.headers.items()))).encode()
    ).hexdigest()
    # Only identical credentials/library share a loading lock. Never hold the
    # global cache lock during a network read for an unrelated account.
    with _lock:
        loading = _loading.setdefault(identity, threading.Lock())
    with loading:
        with _lock:
            cached = _entries.get(identity)
            epoch = _epoch
        if not cached and not policy["refresh"]:
            from .api import job_store
            from .inventory import snapshot

            saved = snapshot(job_store().repository, "plex", identity)
            if saved and time.time() - saved["checked_at"] < policy["seconds"]:
                cached = (time.monotonic() - (time.time() - saved["checked_at"]), saved["rows"])
        fresh = policy["refresh"] and identity not in policy["seen"]
        from . import jobs

        if cached and not fresh and time.monotonic() - cached[0] < policy["seconds"]:
            policy["seen"].add(identity)
            jobs.output(
                f"Using cached Plex library: {len(cached[1])} tracks · age {max(0, int(time.monotonic() - cached[0]))} seconds"
            )
            rows = cached[1]
        else:
            started = time.monotonic()
            rows = fetch()
            cached = (time.monotonic(), rows)
            policy["seen"].add(identity)
            jobs.output(
                f"Plex library loaded: {len(rows)} tracks in {time.monotonic() - started:.2f}s"
            )
        if rows:
            with _lock:
                if epoch == _epoch:
                    _entries[identity] = cached
                    _entries.move_to_end(identity)
                    while len(_entries) > 4:
                        _entries.popitem(last=False)
        return [dict(t) for t in rows]


def invalidate(discard_persistent=False):
    global _epoch
    with _lock:
        _entries.clear()
        _epoch += 1
    if discard_persistent:
        from .api import job_store

        with job_store().repository.connect() as db:
            db.execute("DELETE FROM state WHERE namespace='inventory' AND key='plex'")
