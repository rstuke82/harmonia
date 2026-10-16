"""A replaceable summary derived from the original source description."""

from datetime import datetime
from zoneinfo import ZoneInfo

MARKER = "\n\n— Harmonia —\n"


def readable(value, zone):
    try:
        return (
            datetime.fromisoformat(value)
            .astimezone(ZoneInfo(zone))
            .strftime("%b %d, %Y at %-I:%M %p %Z")
        )
    except (ValueError, TypeError):
        return "Not yet recorded"


def render(description, playlist, source_count, matched_count, missing_count, ignored_count=0):
    from .accounts import personal_repository
    from .playlist_schedules import read, zone

    repo = personal_repository()
    tz = zone(repo)
    schedule = read(repo, f"{playlist.get('source')}:{playlist.get('source_id')}")
    upcoming = (
        "Manual only"
        if schedule["mode"] == "disabled"
        else readable(schedule["next_run"], tz)
        if schedule.get("next_run")
        else "Automatic sync disabled"
    )
    original = (description or "").split(MARKER, 1)[0].rstrip()
    detail = (
        f"Source: {playlist.get('source_url', '')}\n"
        f"Last successful sync: {readable(playlist.get('last_synced'), tz)}\n"
        f"Next scheduled sync: {upcoming}\n"
        f"Tracks: {source_count} source · {matched_count} matched · {missing_count} missing · {ignored_count} ignored"
    )
    return original + MARKER + detail
