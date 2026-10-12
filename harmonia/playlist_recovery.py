"""Serialize writes to the same account/server playlist without retaining locks."""
import threading
import weakref
from contextlib import contextmanager

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()

@contextmanager
def write_lock(key):
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
    with lock:
        yield
