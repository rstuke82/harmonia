"""Back up, then reset only application-owned state in every account."""

import shutil
from fastapi import HTTPException
from .storage import Repository, FILES, read_json
from .atomic_files import _atomic_write_json
from . import reset_guard

MARKER = ".factory-reset.json"


def repositories(root):
    result = [root]
    for folder in sorted((root.directory / "users").glob("*")):
        if folder.name.isdigit() and folder.is_dir() and not folder.is_symlink():
            path = folder / "harmonia.db"
            if path.is_symlink():
                raise HTTPException(409, "A personal database is a symlink; reset refused.")
            if path.exists():
                result.append(Repository(folder))
    return result


def finish(root):
    for repo in repositories(root):
        with repo.connect() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "job_events",
                "jobs",
                "schedules",
                "health_history",
                "diagnostic_events",
                "application_logs",
                "migration_backups",
                "prepared_views",
                "state_revisions",
                "state",
            ):
                db.execute("DELETE FROM " + table)
        startup = read_json(repo.directory / "config.json")
        repo.save_startup(
            {
                k: v
                for k, v in startup.items()
                if repo is root and k in ("data_directory", "data_dir")
            }
        )
        for filename in FILES.values():
            path = repo.directory / filename
            if path.is_symlink():
                path.unlink()
            elif path.is_file():
                path.unlink()
        cache = repo.directory / "plex-image-cache"
        if cache.is_symlink():
            cache.unlink()
        elif cache.is_dir():
            shutil.rmtree(cache)
    from .library import invalidate
    from .library_inventory import clear

    invalidate()
    clear()


def execute(root, manager=None):
    from . import backups

    with reset_guard.exclusive():
        repos = repositories(root)
        if manager and any(w["thread"].is_alive() for w in manager.workers):
            raise HTTPException(409, "Wait for all account workers to stop before resetting.")
        for repo in repos:
            with repo.connect() as db:
                busy = db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running','cancelling') LIMIT 1"
                ).fetchone()
            if busy:
                raise HTTPException(
                    409,
                    "Stop pending jobs in every account and wait for running jobs before resetting.",
                )
        backup = backups.create(root, "before-reset")
        _atomic_write_json(root.directory / MARKER, {"backup": backup["backup"]})
        # Keep the marker until process restart. If interrupted, startup repeats
        # the idempotent reset before any scheduler or authenticated work starts.
        reset_guard.restart_required = True
        finish(root)
        return {
            "reset": True,
            "backup": backup["backup"],
            "message": "Application data reset for all accounts. Restart Harmonia to begin Plex administrator setup.",
        }


def recover(root):
    marker = root.directory / MARKER
    if marker.exists():
        finish(root)
        marker.unlink()
    reset_guard.restart_required = False
