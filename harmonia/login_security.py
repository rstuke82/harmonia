"""Protected installation claiming and database-backed sign-in admission limits."""

import hashlib
import json
import secrets
import time
from fastapi import HTTPException
from .storage import read_json

POLL_INTERVAL = 2
POLL_LEASE = 180  # Covers PIN verification and the bounded Plex account checks.
MAX_ACTIVE_POLLS = 8


def _state(db, namespace, key):
    row = db.execute(
        "SELECT value FROM state WHERE namespace=? AND key=?", (namespace, key)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def _put(db, namespace, key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?)", (namespace, key, json.dumps(value)))


def setup_required(repo):
    plex = read_json(repo.directory / "config.json").get("plex", {})
    return not repo.load("accounts") and not all(
        plex.get(k) for k in ("url", "token", "music_library_key")
    )


def recover_setup(repo):
    """Finish committed ownership/configuration before exposing it or running scans."""
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        pending = _state(db, "installation", "pending_config")
        if pending:
            repo.save_startup(pending["config"])
            from .jobs import Store

            job_id = Store(repo).enqueue("plex_scan", {}, db=db)
            _put(db, "installation", "initial_scan", {"job_id": job_id})
            db.execute("DELETE FROM state WHERE namespace='installation' AND key='pending_config'")
        consumed = _state(db, "installation", "setup").get("consumed", False)
    if consumed:
        (repo.directory / "setup-key").unlink(missing_ok=True)


def initialize_setup(repo):
    """Create a private key once; never put the plaintext key in diagnostic logs."""
    recover_setup(repo)
    if not setup_required(repo):
        return
    from .atomic_files import _atomic_write_text

    generated = None
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM state WHERE namespace='accounts' LIMIT 1").fetchone():
            return
        setup = _state(db, "installation", "setup")
        if setup.get("consumed"):
            raise RuntimeError(
                "Installation ownership exists but account state is missing. Restore a backup."
            )
        path = repo.directory / "setup-key"
        if not setup.get("key_hash"):
            if path.is_symlink():
                raise RuntimeError("Setup key must not be a symlink.")
            # Reuse a durable file if a previous process stopped before DB commit.
            generated = path.read_text().strip() if path.exists() else secrets.token_urlsafe(32)
            if len(generated) < 32:
                raise RuntimeError(
                    "Invalid setup key file. Remove it while Harmonia is stopped and restart."
                )
            _atomic_write_text(path, generated + "\n")
            _put(
                db,
                "installation",
                "setup",
                {"key_hash": hashlib.sha256(generated.encode()).hexdigest(), "consumed": False},
            )
    if generated:
        print(
            f"Harmonia first-time setup key: {generated}\nEnter this key on the setup page. Private recovery file: {repo.directory / 'setup-key'}",
            flush=True,
        )


def authorize_setup(repo, supplied):
    initialize_setup(repo)
    if not setup_required(repo):
        return None
    setup = repo.load_one("installation", "setup", {})
    digest = hashlib.sha256(supplied.strip().encode()).hexdigest()
    if (
        not supplied.strip()
        or setup.get("consumed")
        or not secrets.compare_digest(digest, setup.get("key_hash", ""))
    ):
        raise HTTPException(403, "Enter the setup key from the container startup logs.")
    return digest


def check_setup_grant(repo, grant):
    setup = repo.load_one("installation", "setup", {})
    if (
        not grant
        or setup.get("consumed")
        or not secrets.compare_digest(grant, setup.get("key_hash", ""))
    ):
        raise HTTPException(
            403,
            "This sign-in is not authorized to set up Harmonia. Enter the setup key and start again.",
        )


def commit_setup(repo, config, user, grant):
    """A single transaction elects the owner and durably journals its config."""
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        setup = _state(db, "installation", "setup")
        if (
            setup.get("consumed")
            or db.execute("SELECT 1 FROM state WHERE namespace='accounts' LIMIT 1").fetchone()
        ):
            raise HTTPException(409, "Setup has already been completed. Start a new sign-in.")
        if not grant or not secrets.compare_digest(grant, setup.get("key_hash", "")):
            raise HTTPException(403, "This sign-in is not authorized to set up Harmonia.")
        _put(db, "accounts", str(user["id"]), user)
        _put(db, "installation", "pending_config", {"config": config})
        _put(db, "installation", "setup", {"consumed": True})
        # Other setup attempts may not create a session after ownership changes.
        db.execute(
            "DELETE FROM state WHERE namespace='login_pins' AND json_extract(value,'$.setup_hash') IS NOT NULL"
        )
    recover_setup(repo)


def _rate(db, remote, kind, timestamp, per_ip):
    """Bound each source IP without a shared quota that can starve other users."""
    db.execute(
        "DELETE FROM state WHERE namespace='auth_limits' AND json_extract(value,'$.expires') <= ?",
        (timestamp,),
    )
    # Remove obsolete shared counters; poll concurrency is independently bounded.
    db.execute(
        "DELETE FROM state WHERE namespace='auth_limits' AND key IN ('start:global','poll:global')"
    )
    entries = []
    for key, limit in ((kind + ":" + hashlib.sha256(remote.encode()).hexdigest(), per_ip),):
        recent = [t for t in _state(db, "auth_limits", key).get("times", []) if t > timestamp - 60]
        if len(recent) >= limit:
            raise HTTPException(
                429,
                "Sign-in is busy. Please wait and try again.",
                headers={"Retry-After": str(max(1, int(61 - timestamp + recent[0])))},
            )
        entries.append((key, recent))
    for key, recent in entries:
        _put(db, "auth_limits", key, {"times": [*recent, timestamp], "expires": timestamp + 60})


def admit_start(repo, remote):
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        timestamp = time.time()
        db.execute(
            "DELETE FROM state WHERE namespace IN ('login_pins','sessions') AND json_extract(value,'$.expires') < ?",
            (timestamp,),
        )
        _rate(db, remote, "start", timestamp, 5)


def claim_poll(repo, key, remote):
    """Return a lease for one upstream check, or None for cached/in-flight pending."""
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        timestamp = time.time()
        saved = _state(db, "login_pins", key)
        if not saved or saved.get("expires", 0) < timestamp:
            raise HTTPException(410, "Sign-in expired. Start again.")
        _rate(db, remote, "poll", timestamp, 60)
        if saved.get("lease_until", 0) > timestamp or saved.get("next_check", 0) > timestamp:
            return None
        active = db.execute(
            "SELECT COUNT(*) FROM state WHERE namespace='login_pins' AND json_extract(value,'$.lease_until') > ? AND json_extract(value,'$.expires') > ?",
            (timestamp, timestamp),
        ).fetchone()[0]
        if active >= MAX_ACTIVE_POLLS:
            raise HTTPException(
                429, "Sign-in is busy. Please wait and try again.", headers={"Retry-After": "2"}
            )
        saved.update(
            lease=secrets.token_urlsafe(16),
            lease_until=timestamp + POLL_LEASE,
            next_check=timestamp + POLL_INTERVAL,
        )
        _put(db, "login_pins", key, saved)
        return saved


def release_poll(repo, key, lease):
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        saved = _state(db, "login_pins", key)
        if saved.get("lease") == lease:
            saved.update(lease_until=0, next_check=time.time() + POLL_INTERVAL)
            saved.pop("lease", None)
            _put(db, "login_pins", key, saved)
