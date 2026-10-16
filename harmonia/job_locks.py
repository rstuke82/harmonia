"""Cross-process destination locks independent of a playlist's source identity."""

import hashlib
from contextlib import ExitStack, contextmanager


@contextmanager
def destinations(store, payload):
    from .config import Config
    from .process_lock import ProcessLock
    from .accounts import root_repository

    config = Config(read_only=True, namespaces=[])
    server = config.config.get("plex", {}).get("url", "").rstrip("/")
    selected = payload.get("playlist_keys")
    keys = []
    for p in config.config.get("playlists", []):
        key = f"{p.get('source')}:{p.get('source_id')}"
        if selected is not None and key not in selected:
            continue
        if p.get("plex_playlist_id"):
            keys.append(hashlib.sha256(f"{server}|{p['plex_playlist_id']}".encode()).hexdigest())
    with ExitStack() as stack:
        for key in sorted(set(keys)):
            stack.enter_context(
                ProcessLock(root_repository().directory / "locks" / f"destination-{key}.lock")
            )
        yield
