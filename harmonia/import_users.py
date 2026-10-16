"""Read Plex sharing metadata; importing never copies a Plex credential."""

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from . import network
from fastapi import HTTPException
from pydantic import BaseModel, Field
from . import accounts


def eligible():
    from .storage import read_json

    settings = read_json(accounts.root_repository().directory / "config.json").get("plex", {})
    token = settings.get("token", "")
    identity = accounts.plex_json(
        "GET", settings.get("url", "").rstrip("/") + "/identity", headers=accounts.headers(token)
    )
    machine = identity.get("MediaContainer", {}).get("machineIdentifier")
    if not machine:
        raise HTTPException(502, "Plex server identity unavailable.")

    def xml(url):
        try:
            r = network.get(
                url,
                headers={**accounts.headers(token), "Accept": "application/xml"},
                timeout=(5, 20),
            )
            r.raise_for_status()
            return ET.fromstring(r.content, forbid_dtd=True)
        except (network.RequestException, ET.ParseError, DefusedXmlException) as exc:
            raise HTTPException(
                502, "Could not read Plex sharing permissions. No users were imported."
            ) from exc

    users = xml("https://plex.tv/api/users")
    result = []
    for user in users.findall(".//User"):
        a = user.attrib
        if not a.get("email") or a.get("restricted") == "1" or not a.get("id", "").isdigit():
            continue
        for server in user.findall("./Server"):
            if server.get("machineIdentifier") != machine or server.get("pending") == "1":
                continue
            allowed = server.get("allLibraries") == "1"
            if not allowed:
                share = xml(
                    f"https://plex.tv/api/servers/{machine}/shared_servers/{server.get('id')}"
                )
                allowed = any(
                    s.get("key") == str(settings.get("music_library_key"))
                    and s.get("shared") == "1"
                    for s in share.findall(".//Section")
                )
            if allowed:
                result.append(
                    {
                        "id": a["id"],
                        "name": a.get("title") or a.get("username") or a["email"],
                        "avatar": a.get("thumb", ""),
                        "existing": a["id"] in accounts.users(),
                    }
                )
                break
    return result


def register(app):
    @app.get("/api/users/import")
    def preview():
        return eligible()

    class Selection(BaseModel):
        ids: list[str] = Field(min_length=1, max_length=500)

    @app.post("/api/users/import")
    def apply(body: Selection):
        candidates = {u["id"]: u for u in eligible()}
        existing = accounts.users()
        count = 0
        if any(i not in candidates for i in body.ids):
            raise HTTPException(409, "Plex sharing changed. Refresh the list.")
        for key in set(body.ids):
            if key in existing:
                continue
            accounts.put(
                "accounts",
                key,
                {
                    **candidates[key],
                    "admin": False,
                    "can_request": True,
                    "auto_approve": False,
                    "can_playlists": True,
                    "disabled": False,
                    "pending_login": True,
                },
            )
            count += 1
        accounts.root_repository().add_log(
            "INFO",
            "Users",
            f"Administrator imported {count} Plex users; waiting for their own sign-in",
        )
        return {"imported": count}
