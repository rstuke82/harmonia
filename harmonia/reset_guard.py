"""Coordinate factory reset with HTTP requests and the scheduler."""
import threading
from contextlib import contextmanager
from fastapi import HTTPException

scheduler_lock=threading.RLock()
_condition=threading.Condition()
_requests=0
_resetting=False
restart_required=False

def enter_request():
    global _requests
    with _condition:
        if _resetting or restart_required:
            return False
        _requests+=1
        return True

def leave_request():
    global _requests
    with _condition:
        _requests-=1

@contextmanager
def exclusive():
    global _resetting
    with _condition:
        if _resetting or _requests>1:
            raise HTTPException(409,'Other requests are active. Wait for them to finish before resetting.')
        _resetting=True
    try:
        with scheduler_lock:yield
    finally:
        with _condition:_resetting=False
