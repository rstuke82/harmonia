"""Task management, backup controls and bulk playlist preferences."""

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal


class AutoSyncRequest(BaseModel):
    playlist_keys: list[str] = Field(min_length=1)
    auto_sync: bool


class BackupSettings(BaseModel):
    retention: Literal[1, 3, 7] = 7


class RestoreRequest(BaseModel):
    name: str


class AliasRequest(BaseModel):
    canonical: str = Field(min_length=1)
    aliases: list[str]


class CacheRequest(BaseModel):
    group: Literal[
        "library_views", "musicbrainz", "metadata", "artwork", "inventories", "file_audit"
    ]


class DatabaseSettings(BaseModel):
    job_days: int = Field(default=90, ge=7, le=3650)
    log_days: int = Field(default=30, ge=1, le=3650)


class FactoryResetRequest(BaseModel):
    confirmation: str


def register(app):
    from .api import job_store, _config
    from .process_lock import ProcessLock
    from . import tasks, backups

    @app.get("/api/tasks")
    def list_tasks():
        store = job_store()
        return {
            "tasks": tasks.rows(store),
            "startup_views": store.repository.load("task_startup").get("page_views", "outdated"),
            **store.repository.load("tasks").get("initialized", {"timezone": "UTC"}),
            "migration_notes": store.repository.load("tasks")
            .get("sync_modes_v3", {})
            .get("notes", []),
        }

    @app.post("/api/tasks/restore-defaults")
    def restore_task_defaults(action: str = ""):
        try:
            return {"tasks": tasks.restore_defaults(job_store(), action or None)}
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/job-history")
    def history(
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        owner: str = "",
        worker: str = "",
        sections: bool = False,
    ):
        from .jobs import WORKER_LIMITS

        if worker and worker not in WORKER_LIMITS:
            raise HTTPException(422, "Unknown worker")
        from .accounts import actor

        if actor() and actor().get("admin"):
            from .admin_activity import history as all_history

            return all_history(offset, limit, owner, worker, sections=sections)
        from .jobs import history_where
        from .job_summaries import projection

        columns = projection()
        where, params = history_where(worker)
        store = job_store()
        active = []
        if sections:
            active = store._rows(
                "SELECT "
                + columns
                + " FROM jobs"
                + where
                + " AND status IN ('running','cancelling','queued') ORDER BY created_at ASC,id ASC",
                params,
            )
            where += " AND status NOT IN ('running','cancelling','queued')"
        with store.repository.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM jobs" + where, params).fetchone()[0]
        order = (
            "COALESCE(finished_at,started_at,created_at) DESC,created_at DESC,id ASC"
            if sections
            else "created_at DESC"
        )
        result = {
            "rows": store._rows(
                "SELECT "
                + columns
                + " FROM jobs"
                + where
                + " ORDER BY "
                + order
                + " LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ),
            "total": total,
        }
        if sections:
            result["active"] = active
        return result

    @app.post("/api/playlists/auto-sync", status_code=202)
    def set_auto_sync(request: AutoSyncRequest):
        raise HTTPException(410, "Auto Sync flags have been replaced by playlist Sync mode")

    @app.get("/api/backups")
    def get_backups():
        repo = job_store().repository
        return {
            "backups": backups.listing(repo),
            "retention": min(7, repo.load("backup_settings").get("retention", 7)),
        }

    @app.put("/api/backups/settings")
    def backup_settings(request: BackupSettings):
        job_store().repository.save({"backup_settings": request.model_dump()})
        return request.model_dump()

    @app.delete("/api/backups/{name}")
    def delete_backup(name: str):
        store = job_store()
        if any(
            j["action"] in ("backup", "restore_backup")
            and j["status"] in ("queued", "running", "cancelling")
            for j in store.list()
        ):
            raise HTTPException(
                409, "Wait for backup or restore to finish before deleting a backup."
            )
        try:
            with ProcessLock():
                path = backups.path_for(store.repository, name)
                if name not in {b["name"] for b in backups.listing(store.repository)}:
                    raise ValueError("Backup not recognized")
                path.unlink()
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            raise HTTPException(
                409,
                "A Harmonia operation is using the data store. Try deleting the backup after it finishes.",
            ) from exc
        store.repository.add_log("INFO", "Backup", f"Deleted backup {name}")
        return {"deleted": name}

    @app.get("/api/backups/{name}/download")
    def download(name: str):
        try:
            path = backups.path_for(job_store().repository, name)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.post("/api/backups/restore", status_code=202)
    def restore(request: RestoreRequest):
        store = job_store()
        try:
            backups.path_for(store.repository, request.name)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        return store.get(store.enqueue("restore_backup", {"name": request.name}))

    @app.get("/api/database")
    def database_status(check: bool = False):
        from .database_maintenance import status

        return status(job_store().repository, check)

    @app.post("/api/database/cache")
    def clear_database_cache(request: CacheRequest):
        from .database_maintenance import clear_cache

        try:
            return clear_cache(job_store().repository, request.group)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/database/cleanup", status_code=202)
    def database_cleanup(request: DatabaseSettings):
        store = job_store()
        store.repository.save({"database_maintenance": {"settings": request.model_dump()}})
        return store.get(store.enqueue("database_cleanup", request.model_dump()))

    @app.post("/api/database/factory-reset")
    def factory_reset(request: FactoryResetRequest):
        if request.confirmation != "RESET HARMONIA":
            raise HTTPException(422, "Type RESET HARMONIA exactly.")
        from .factory_reset import execute

        store = job_store()
        return execute(store.repository, getattr(app.state, "jobs", None))

    @app.get("/api/settings/matching")
    def matching():
        return {"aliases": _config(read_only=True).artist_aliases}

    @app.put("/api/settings/matching/alias")
    def save_alias(request: AliasRequest):
        with ProcessLock():
            config = _config()
            canonical = request.canonical.strip()
            if not canonical:
                raise HTTPException(400, "Enter an artist name")
            aliases = list(dict.fromkeys(a.strip() for a in request.aliases if a.strip()))
            if aliases:
                config.artist_aliases[canonical] = aliases
            else:
                config.artist_aliases.pop(canonical, None)
            config.save_artist_aliases()
        return {"aliases": config.artist_aliases}
