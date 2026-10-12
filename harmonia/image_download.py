"""Bounded image streaming; timeouts cannot leave unbounded background readers."""
from dataclasses import dataclass
import queue
import threading
import time
import requests
from fastapi import HTTPException

_downloads=threading.BoundedSemaphore(4)
_callers=threading.BoundedSemaphore(24)
_flights={}
_flight_lock=threading.Lock()


@dataclass
class ImageBytes:
    content: bytes
    headers: dict


def _stream_download(url, *, headers=None, max_bytes=20_000_000, oversized_status=413, seconds=30, external=False):
    started=time.monotonic()
    if not _downloads.acquire(timeout=min(2,seconds)):
        raise HTTPException(503,'Artwork downloads are busy. Please retry.',headers={'Retry-After':'2'})
    result=queue.Queue(maxsize=1);cancelled=threading.Event();deadline=started+seconds
    def read():
        response=None
        try:
            from .redirect_policy import get
            response=get(url,headers=headers,external=external)
            response.raise_for_status()
            try:length=int(response.headers.get('Content-Length','0'))
            except (TypeError,ValueError):length=0
            if length>max_bytes:raise HTTPException(oversized_status,'The image is too large.')
            content=bytearray()
            for chunk in response.iter_content(chunk_size=64*1024):
                if cancelled.is_set() or time.monotonic()>deadline:raise HTTPException(504,'Artwork download timed out. Please retry.')
                if len(content)+len(chunk)>max_bytes:raise HTTPException(oversized_status,'The image is too large.')
                content.extend(chunk)
            if cancelled.is_set() or time.monotonic()>deadline:raise HTTPException(504,'Artwork download timed out. Please retry.')
            outcome=ImageBytes(bytes(content),dict(response.headers))
        except Exception as exc:
            outcome=exc
        finally:
            try:
                if response is not None:response.close()
            except Exception as exc:
                if not isinstance(outcome,Exception):outcome=exc
            finally:_downloads.release()
        result.put(outcome)
    try:
        thread=threading.Thread(target=read,name='harmonia-image-download',daemon=True);thread.start()
    except Exception:
        _downloads.release();raise
    try:outcome=result.get(timeout=max(.001,deadline-time.monotonic()))
    except queue.Empty:
        # The caller returns immediately even if an upstream dribbles bytes inside
        # a blocked read. That reader retains its slot until it exits; never spawn
        # more than four outstanding readers, including timed-out ones.
        cancelled.set()
        raise HTTPException(504,'Artwork download timed out. Please retry.') from None
    if isinstance(outcome,Exception):raise outcome
    return outcome


def download(url, *, headers=None, max_bytes=20_000_000, oversized_status=413, seconds=30, external=False):
    """Share identical in-flight requests without mixing accounts or policies."""
    if not _callers.acquire(blocking=False):
        raise HTTPException(503,'Artwork downloads are busy. Please retry.',headers={'Retry-After':'2'})
    key=(url,tuple(sorted((str(k),str(v)) for k,v in (headers or {}).items())),max_bytes,oversized_status,external)
    owner=False
    try:
        with _flight_lock:
            flight=_flights.get(key)
            if flight is None:
                flight={'event':threading.Event()};_flights[key]=flight;owner=True
        if owner:
            try:flight['result']=_stream_download(url,headers=headers,max_bytes=max_bytes,oversized_status=oversized_status,seconds=seconds,external=external)
            except Exception as exc:flight['result']=exc
            finally:
                with _flight_lock:
                    flight['event'].set();_flights.pop(key,None)
        elif not flight['event'].wait(seconds):
            raise HTTPException(504,'Artwork download timed out. Please retry.')
        result=flight['result']
        if isinstance(result,Exception):raise result
        return result
    finally:_callers.release()
