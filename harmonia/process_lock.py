"""Account-aware, cancellable process locking for background work."""
from pathlib import Path
import fcntl
import hashlib
import os
import tempfile
import time
from .runtime_paths import CONFIG_DIR

def _process_lock_path() -> Path:
    """Return a per-working-directory lock path outside the Git checkout."""
    identity = str(CONFIG_DIR.resolve())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"harmonia-{digest}.lock"


class ProcessLock:
    """
    Kernel-backed single-process lock.

    The temp lock file may remain after exit, but flock itself is released by
    the kernel on normal exit, crashes, or SIGKILL, so stale files do not block
    future Harmonia runs.
    """

    def __init__(self, path: Path = None, wait: bool = None):
        from .accounts import personal_repository, actor, is_owner
        self.path = Path(path) if path is not None else (personal_repository().directory / '.process.lock' if actor() and not is_owner(actor()) else _process_lock_path())
        self._handle = None
        # Background jobs wait at a cancellable checkpoint; interactive callers
        # retain immediate busy errors. Explicit wait=False remains supported.
        from . import jobs
        self.wait = bool(jobs.current()) if wait is None else wait

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+", encoding="utf-8")

        try:
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not self.wait:
                        raise
                    from . import jobs
                    if jobs.current():
                        jobs.current().checkpoint()
                    time.sleep(0.1)
        except BlockingIOError:
            self._handle.seek(0)
            owner = self._handle.read().strip()
            owner_text = f" (PID {owner})" if owner else ""
            self._handle.close()
            self._handle = None
            raise RuntimeError(
                "Another Harmonia process is already running"
                f"{owner_text}. Wait for it to finish first."
            )
        except BaseException:
            self._handle.close()
            self._handle = None
            raise

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._handle is None:
            return False
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
        return False


