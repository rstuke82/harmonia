"""Durable atomic writes for JSON exports and startup configuration."""

from pathlib import Path
import json
import os
import tempfile


def _fsync_parent_directory(path: Path):
    """Best-effort fsync of the containing directory after os.replace()."""
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str):
    """Atomically replace a UTF-8 text file in its destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, path)
        _fsync_parent_directory(path)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _atomic_write_json(path: Path, data, *, ensure_ascii: bool = True):
    """Serialize JSON then atomically replace the destination file."""
    payload = (
        json.dumps(
            data,
            indent=2,
            ensure_ascii=ensure_ascii,
        )
        + "\n"
    )
    _atomic_write_text(path, payload)
