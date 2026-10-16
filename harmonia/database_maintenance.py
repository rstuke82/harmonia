"""Reviewed SQLite maintenance actions; source media and Plex are never modified."""

from datetime import datetime, timezone, timedelta
import json
import time

CACHE_GROUPS = {
    "library_views": (),
    "musicbrainz": ("musicbrainz_cache", "library_artist_catalog", "library_artist_metadata"),
    "metadata": ("library_lastfm_artist_metadata", "local_artist_enrichment"),
    "artwork": ("fanart_cache",),
    "inventories": ("inventory", "inventory_links"),
    "file_audit": ("file_audit",),
}


def status(repo, run_checks=False):
    wal = repo.path.with_name(repo.path.name + "-wal")
    shm = repo.path.with_name(repo.path.name + "-shm")
    with repo.connect() as db:
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        pages = int(db.execute("PRAGMA page_count").fetchone()[0])
        free = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        journal = str(db.execute("PRAGMA journal_mode").fetchone()[0])
        quick = db.execute("PRAGMA quick_check").fetchone()[0] if run_checks else None
        foreign = len(db.execute("PRAGMA foreign_key_check").fetchall()) if run_checks else None
        cache_groups = {
            name: {
                "entries": sum(
                    int(
                        db.execute(
                            "SELECT COUNT(*) FROM state WHERE namespace=?", (namespace,)
                        ).fetchone()[0]
                    )
                    for namespace in namespaces
                )
            }
            for name, namespaces in CACHE_GROUPS.items()
        }
    with repo.connect() as db:
        cache_groups["library_views"]["entries"] = db.execute(
            "SELECT COUNT(*) FROM prepared_views"
        ).fetchone()[0]
    saved = repo.load_one("database_maintenance", "settings", {})
    return {
        "database_bytes": repo.path.stat().st_size if repo.path.exists() else 0,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "shm_bytes": shm.stat().st_size if shm.exists() else 0,
        "page_size": page_size,
        "pages": pages,
        "free_pages": free,
        "reclaimable_bytes": page_size * free,
        "schema_version": version,
        "journal_mode": journal,
        "quick_check": quick,
        "foreign_key_issues": foreign,
        "cache_groups": cache_groups,
        "last_maintenance": repo.load_one("database_maintenance", "last", {}),
        "settings": {
            "job_days": int(saved.get("job_days", 90)),
            "log_days": int(saved.get("log_days", 30)),
        },
    }


def clear_cache(repo, group):
    if group not in CACHE_GROUPS:
        raise ValueError("Unknown cache group.")
    namespaces = CACHE_GROUPS[group]
    deleted = 0
    if group == "library_views":
        from .accounts import root_repository, personal_repository, users, as_user

        repositories = {str(repo.directory): repo}
        if repo.directory == root_repository().directory:
            for user in users().values():
                with as_user(user):
                    candidate = personal_repository()
                    repositories[str(candidate.directory)] = candidate
        from .saved_views import _lock

        with _lock:
            for candidate in repositories.values():
                with candidate.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    deleted += db.execute("DELETE FROM prepared_views").rowcount
                    db.execute(
                        "INSERT OR REPLACE INTO state VALUES('library_views_epoch','clear',?)",
                        (json.dumps({"at": time.time()}),),
                    )
    if namespaces:
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM state WHERE namespace IN (" + ",".join("?" for _ in namespaces) + ")",
                namespaces,
            ).rowcount
    from .library import invalidate

    invalidate()
    return {
        "cleared": group,
        "deleted": max(0, deleted),
        "message": "Cleared " + group.replace("_", " ") + " cache.",
    }


def _backup(repo, kind):
    from . import backups

    return backups.create(repo, kind)


def execute(action, payload=None):
    from .api import job_store
    from . import jobs

    repo = job_store().repository
    payload = payload or {}
    backup = None
    if action == "database_optimize":
        jobs.progress("Optimizing database query statistics")
        with repo.connect() as db:
            db.execute("PRAGMA optimize")
        jobs.progress("Checkpointing the database journal")
        with repo.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        summary = "Database optimized and journal checkpointed."
    elif action == "database_compact":
        jobs.progress("Creating a safety backup before compaction")
        backup = _backup(repo, "before-maintenance")
        jobs.progress("Compacting the database")
        with repo.connect() as db:
            db.execute("VACUUM")
        summary = "Database backup completed and unused pages compacted."
    elif action == "database_cleanup":
        job_days = max(7, min(3650, int(payload.get("job_days", 90))))
        log_days = max(1, min(3650, int(payload.get("log_days", 30))))
        jobs.progress("Creating a safety backup before history cleanup")
        backup = _backup(repo, "before-maintenance")
        job_cut = (datetime.now(timezone.utc) - timedelta(days=job_days)).isoformat()
        log_cut = (datetime.now(timezone.utc) - timedelta(days=log_days)).isoformat()
        with repo.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') AND created_at<?",
                    (job_cut,),
                )
            ]
            if old:
                db.execute(
                    "DELETE FROM job_events WHERE job_id IN (" + ",".join("?" for _ in old) + ")",
                    old,
                )
            if old:
                placeholders = ",".join("?" for _ in old)
                db.execute(
                    "DELETE FROM state WHERE namespace='job_result_summaries' AND key IN ("
                    + placeholders
                    + ")",
                    old,
                )
                db.execute("DELETE FROM jobs WHERE id IN (" + placeholders + ")", old)
            diagnostics = db.execute(
                "DELETE FROM diagnostic_events WHERE created_at<?", (log_cut,)
            ).rowcount
            logs = db.execute(
                "DELETE FROM application_logs WHERE created_at<?", (log_cut,)
            ).rowcount
            now = time.time()
            sessions = db.execute(
                "DELETE FROM state WHERE namespace IN ('sessions','login_pins') AND COALESCE(json_extract(value,'$.expires'),0)<?",
                (now,),
            ).rowcount
            db.execute(
                "INSERT OR REPLACE INTO state VALUES('database_maintenance','settings',?)",
                (json.dumps({"job_days": job_days, "log_days": log_days}),),
            )
        summary = f"Removed {len(old)} old jobs, {diagnostics + logs} old log entries, and {sessions} expired sessions."
    elif action == "database_rebuild":
        jobs.progress("Clearing derived library links and prepared views")
        clear_cache(repo, "library_views")
        with repo.connect() as db:
            db.execute("DELETE FROM state WHERE namespace='inventory_links'")
        summary = "Derived Library views and inventory links were cleared. Scheduled scans will rebuild them."
    else:
        raise ValueError("Unknown database maintenance action.")
    from .lidarr import state_put

    state_put(
        repo,
        "database_maintenance",
        "last",
        {"action": action, "completed_at": time.time(), "summary": summary},
    )
    return {"summary": summary, "backup": backup.get("backup") if backup else None}
