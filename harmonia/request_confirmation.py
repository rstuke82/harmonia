"""Use targeted album confirmation without mistaking import status for availability."""


def apply(record, info, server):
    evidence = record.get("plex_confirmation") or {}
    if evidence.get("server") != server or record.get("removed_from_lidarr"):
        return info
    if evidence.get("webhook_at") != record.get("webhook_checked_at"):
        return info
    if record.get("webhook_failure") or record.get("download_error"):
        return info
    found = evidence.get("found", 0)
    expected = evidence.get("expected", 0)
    if not found:
        return info
    checked = evidence.get("checked_at", 0)
    if info.get("availability") != "Unknown" and (info.get("checked_at") or 0) > checked:
        return info
    full = expected > 0 and found >= expected
    return {
        **info,
        "availability": "Available" if full else "Partially Available",
        "availability_source": "targeted Plex check",
        "checked_at": checked,
        "availability_detail": f"Targeted Plex check confirmed {found}/{expected or 'unknown'} tracks.",
    }
