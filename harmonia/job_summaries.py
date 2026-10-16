"""Small polling projections; complete results stay available on job detail reads."""

import json

FIELDS = (
    "summary",
    "partial_success",
    "pending",
    "key",
    "name",
    "total",
    "matched",
    "unresolved",
    "lost",
    "backup",
    "restored",
    "safety_backup",
)


def compact(result):
    if not isinstance(result, dict):
        return None
    value = {key: result[key] for key in FIELDS if key in result}
    if isinstance(value.get("summary"), str):
        value["summary"] = value["summary"][:2000]
    if isinstance(result.get("playlists"), list):
        # Completion keys are retained so a playlist updates immediately within
        # a running batch, without returning its source previews or full health.
        value["playlists"] = [
            {key: row[key] for key in ("key", "ok", "name") if key in row}
            for row in result["playlists"]
            if isinstance(row, dict)
        ]
    return value


def projection():
    # Old results need no bulk migration or startup rewrite. SQLite extracts the
    # small fallback without sending full blobs through Python/the event loop.
    fields = ",".join(f"'{key}',json_extract(jobs.result,'$.{key}')" for key in FIELDS)
    playlists = """(SELECT json_group_array(json_object(
        'key',json_extract(value,'$.key'),'ok',json_extract(value,'$.ok'),
        'name',json_extract(value,'$.name'))) FROM json_each(jobs.result,'$.playlists'))"""
    result = f"""CASE WHEN jobs.result IS NULL THEN NULL ELSE COALESCE(
        (SELECT value FROM state WHERE namespace='job_result_summaries' AND key=jobs.id),
        json_object({fields},'playlists',json({playlists}))) END AS result"""
    columns = ",".join(
        "jobs." + key
        for key in (
            "id",
            "action",
            "payload",
            "status",
            "progress",
            "created_at",
            "started_at",
            "finished_at",
            "error",
            "cancel_requested",
            "schedule_id",
        )
    )
    return columns + "," + result


def save(db, job_id, result):
    db.execute(
        "INSERT INTO state(namespace,key,value) VALUES('job_result_summaries',?,?) "
        "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
        (job_id, json.dumps(compact(result), default=str)),
    )
