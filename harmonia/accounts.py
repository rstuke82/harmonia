"""Plex PIN login, opaque sessions, and isolated personal SQLite stores.

The existing root store belongs to the verified server owner. Other accounts
never inherit its playlists or Plex token. Background work carries an explicit
account context, independent of the HTTP session that queued it.
"""

import contextlib
import contextvars
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode
from . import network
from fastapi import HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from .browser_security import secure_cookie
from .manual_matches import RemoveManualMatch

_actor = contextvars.ContextVar("bridge_account", default=None)
COOKIE = "bridge_session"
PIN_COOKIE = "bridge_login"
CLIENT = "harmonia-3"


def root_repository():
    from .storage import get_repository
    from .runtime_paths import CONFIG_DIR

    return get_repository(CONFIG_DIR)


def actor():
    return _actor.get()


@contextlib.contextmanager
def as_user(user):
    token = _actor.set(user)
    try:
        yield
    finally:
        _actor.reset(token)


def is_owner(user):
    # Pre-beta-3 administrators were exclusively the verified server owner.
    return bool(
        user and (user.get("server_context") or user.get("owner", user.get("admin", False)))
    )


def personal_repository():
    user = actor()
    root = root_repository()
    if not user or is_owner(user):
        return root
    from .storage import get_repository

    return get_repository(root.directory / "users" / str(int(user["id"])))


def public(user):
    from .lidarr import config

    user = {
        **user,
        "owner": is_owner(user),
        "can_library_issues": bool(user.get("admin") or user.get("can_library_issues")),
        "lidarr_enabled": bool(config(root_repository()).get("enabled")),
    }
    return {
        k: user.get(k)
        for k in (
            "id",
            "name",
            "avatar",
            "admin",
            "can_request",
            "can_view_all_requests",
            "can_playlists",
            "can_library_issues",
            "lidarr_enabled",
            "disabled",
            "pending_login",
            "owner",
            "can_downloads",
            "can_manage_downloads",
            "auto_approve",
        )
    }


def api_key_status(user):
    return {
        "configured": bool(user.get("api_key_hash")),
        "scopes": user.get("api_key_scopes", ["read"]),
        "prefix": user.get("api_key_prefix", ""),
        "created_at": user.get("api_key_created_at"),
        "last_used_at": user.get("api_key_last_used_at"),
    }


API_SCOPES = {"read", "playlists:write", "requests:write", "webhooks:lidarr"}


def _key_scopes(user, scopes):
    scopes = set(scopes) | {"read"}
    if not scopes <= API_SCOPES:
        raise HTTPException(422, "Unknown API key permission.")
    if "webhooks:lidarr" in scopes and not user.get("admin"):
        raise HTTPException(403, "Only an administrator can enable Lidarr webhooks.")
    return sorted(scopes)


def _key_account(db, user):
    row = db.execute(
        "SELECT value FROM state WHERE namespace='accounts' AND key=?", (str(user["id"]),)
    ).fetchone()
    if not row:
        raise HTTPException(401, "Account no longer exists.")
    current = json.loads(row[0])
    if current.get("disabled"):
        raise HTTPException(403, "Account is disabled.")
    return current


def create_api_key(user, replace=False):
    """Keys start read-only; regeneration retains explicitly granted scopes."""
    with root_repository().connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _key_account(db, user)
        if current.get("api_key_hash") and not replace:
            return api_key_status(current) | {"key": ""}
        token = "pb_live_" + secrets.token_urlsafe(32)
        current.update(
            api_key_hash=hashlib.sha256(token.encode()).hexdigest(),
            api_key_prefix=token[:16] + "…",
            api_key_created_at=time.time(),
            api_key_last_used_at=None,
            api_key_scopes=_key_scopes(current, current.get("api_key_scopes", ["read"])),
        )
        db.execute(
            "UPDATE state SET value=? WHERE namespace='accounts' AND key=?",
            (json.dumps(current), str(user["id"])),
        )
    root_repository().add_log(
        "INFO",
        "API access",
        f"API key {'regenerated' if replace else 'created'} for account {user['id']}",
    )
    return api_key_status(current) | {"key": token}


def set_api_key_scopes(user, scopes):
    with root_repository().connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _key_account(db, user)
        if not current.get("api_key_hash"):
            raise HTTPException(409, "Create an API key first.")
        scopes = _key_scopes(current, scopes)
        db.execute(
            "UPDATE state SET value=json_set(value,'$.api_key_scopes',json(?)) WHERE namespace='accounts' AND key=?",
            (json.dumps(scopes), str(user["id"])),
        )
        current["api_key_scopes"] = scopes
    return api_key_status(current)


def revoke_api_key(user):
    with root_repository().connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _key_account(db, user)
        for key in (
            "api_key_hash",
            "api_key_prefix",
            "api_key_created_at",
            "api_key_last_used_at",
            "api_key_scopes",
        ):
            current.pop(key, None)
        db.execute(
            "UPDATE state SET value=? WHERE namespace='accounts' AND key=?",
            (json.dumps(current), str(user["id"])),
        )
    root_repository().add_log("INFO", "API access", f"API key revoked for account {user['id']}")


def _request_api_token(request):
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode()
            return decoded.partition(":")[2]
        except Exception:
            return ""
    return request.headers.get("x-api-key", "").strip()


def api_user(request):
    token = _request_api_token(request)
    if not token or not token.startswith("pb_live_"):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    repo = root_repository()
    query = "SELECT key,value FROM state WHERE namespace='accounts' AND json_extract(value,'$.api_key_hash')=?"
    with repo.connect() as db:
        row = db.execute(query, (digest,)).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid Harmonia API key.")
    key, raw = row
    user = json.loads(raw)
    if not secrets.compare_digest(user.get("api_key_hash", ""), digest):
        raise HTTPException(401, "Invalid Harmonia API key.")
    if user.get("disabled"):
        raise HTTPException(403, "This account has been disabled by the administrator.")
    now = time.time()
    if now - float(user.get("api_key_last_used_at") or 0) <= 60:
        return user
    # Recheck under the write lock: never revive a disabled account or rotated key.
    with repo.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (digest,)).fetchone()
        if row is None:
            raise HTTPException(401, "Invalid Harmonia API key.")
        key, raw = row
        user = json.loads(raw)
        if user.get("disabled"):
            raise HTTPException(403, "This account has been disabled by the administrator.")
        if now - float(user.get("api_key_last_used_at") or 0) > 60:
            db.execute(
                "UPDATE state SET value=json_set(value,'$.api_key_last_used_at',?) WHERE namespace='accounts' AND key=?",
                (now, key),
            )
            user["api_key_last_used_at"] = now
        return user


def put(namespace, key, value):
    root_repository().put(namespace, key, value)


def delete(namespace, key):
    root_repository().delete(namespace, key)


def users():
    return root_repository().load("accounts")


def headers(token=None):
    result = {
        "Accept": "application/json",
        "X-Plex-Product": "Harmonia",
        "X-Plex-Client-Identifier": CLIENT,
    }
    if token:
        result["X-Plex-Token"] = token
    return result


def plex_json(method, url, **kwargs):
    try:
        response = network.request(
            method,
            url,
            timeout=(5, 15),
            **kwargs,
            allow_redirects=False,
            _service="Plex sign-in",
            _log_body=False,
        )
        if response.status_code in (401, 403):
            raise HTTPException(403, "Plex did not authorize this account.")
        response.raise_for_status()
        return response.json()
    except network.RequestException as exc:
        raise HTTPException(
            503, "Plex login is temporarily unavailable. Please try again."
        ) from exc


def verify(token, setup_hash=None, *, choose_library=False, library_key=None):
    from .storage import read_json

    root = root_repository()
    from .login_security import recover_setup, setup_required, check_setup_grant, commit_setup

    recover_setup(root)
    initializing = setup_required(root)
    if initializing:
        check_setup_grant(root, setup_hash)
    elif setup_hash:
        raise HTTPException(409, "Setup has already been completed. Start a new sign-in.")
    settings = read_json(root.directory / "config.json").get("plex", {})
    startup = None
    profile = plex_json("GET", "https://plex.tv/api/v2/user", headers=headers(token))
    if not profile.get("email") or profile.get("restricted") or profile.get("guest"):
        raise HTTPException(403, "Use an individual Plex account. Managed users are not supported.")
    configured = bool(
        settings.get("url") and settings.get("token") and settings.get("music_library_key")
    )
    machine = ""
    if configured:
        identity = plex_json(
            "GET", settings["url"].rstrip("/") + "/identity", headers=headers(settings["token"])
        )
        machine = identity.get("MediaContainer", {}).get("machineIdentifier")
    resources = plex_json(
        "GET",
        "https://plex.tv/api/v2/resources",
        headers=headers(token),
        params={"includeHttps": 1},
    )
    resource = next(
        (
            r
            for r in resources
            if machine
            and r.get("clientIdentifier") == machine
            and "server" in r.get("provides", "").split(",")
        ),
        None,
    )
    if not configured:
        if users():
            raise HTTPException(
                409,
                "Plex server setup is incomplete. An administrator must repair the Plex connection.",
            )
        resource = next(
            (
                r
                for r in resources
                if r.get("owned") is True and "server" in r.get("provides", "").split(",")
            ),
            None,
        )
        if not resource:
            raise HTTPException(
                403, "The Plex server owner must sign in first to initialize Harmonia."
            )
        from .plex_setup import music_connection

        server_url, music = music_connection(resource, token, all_libraries=True)
        if choose_library and not library_key:
            return {
                "setup_libraries": [
                    {"key": str(s["key"]), "title": s.get("title", "Music")} for s in music
                ]
            }
        if library_key:
            music = next((s for s in music if str(s["key"]) == library_key), None)
            if music is None:
                raise HTTPException(403, "Choose an accessible Plex music library.")
        else:
            music = music[0]
        server_token = resource.get("accessToken") or token
        startup = read_json(root.directory / "config.json")
        startup["plex"] = {
            "url": server_url,
            "token": server_token,
            "music_library_key": str(music["key"]),
            "music_library_title": music.get("title", "Music"),
        }
        settings = startup["plex"]
        configured = True
    if not resource or not resource.get("accessToken"):
        raise HTTPException(403, "This account does not have access to the configured Plex server.")
    server_token = resource["accessToken"]
    libraries = plex_json(
        "GET", settings["url"].rstrip("/") + "/library/sections", headers=headers(server_token)
    )
    if not any(
        str(s.get("key")) == str(settings["music_library_key"]) and s.get("type") == "artist"
        for s in libraries.get("MediaContainer", {}).get("Directory", [])
    ):
        raise HTTPException(
            403, "This account does not have access to the configured music library."
        )
    user_id = str(int(profile["id"]))
    existing = users().get(user_id, {})
    if existing.get("disabled"):
        raise HTTPException(403, "This account has been disabled by the administrator.")
    owner = resource.get("owned") is True
    if not users() and not owner:
        raise HTTPException(403, "The Plex server owner must sign in first to initialize Harmonia.")
    # Only the server owner may inherit the pre-3.0 root library and settings.
    user = {
        "id": user_id,
        "name": profile.get("username") or profile.get("title") or user_id,
        "avatar": profile.get("thumb", ""),
        "owner": owner,
        "admin": owner or existing.get("admin", False),
        "can_downloads": existing.get("can_downloads", False),
        "can_manage_downloads": existing.get("can_manage_downloads", False),
        "auto_approve": existing.get("auto_approve", owner),
        "can_view_all_requests": existing.get("can_view_all_requests", False),
        "can_request": existing.get("can_request", True),
        "can_playlists": existing.get("can_playlists", True),
        "can_library_issues": existing.get("can_library_issues", False),
        "pending_login": False,
        "disabled": False,
        "plex_token": server_token,
        "verified_at": time.time(),
    }
    # Plex reauthentication refreshes identity and access, but must never rotate
    # the non-expiring automation credential. Rotation is an explicit account action.
    for field in (
        "api_key_hash",
        "api_key_prefix",
        "api_key_created_at",
        "api_key_last_used_at",
        "api_key_scopes",
    ):
        if field in existing:
            user[field] = existing[field]
    if initializing:
        commit_setup(root, startup, user, setup_hash)
        root.add_log(
            "INFO",
            "First-run setup",
            "Owner configured; initial Plex library scan queued.",
            service="Plex",
        )
    else:
        put("accounts", user_id, user)
    root.add_log("INFO", "Plex sign-in", f"Account {user_id} signed in; server owner={owner}")
    if (not existing or existing.get("pending_login")) and not initializing:
        from .jobs import Store

        with as_user(user):
            job_id = Store(personal_repository()).enqueue("plex_scan", {})
            if owner:
                put("installation", "initial_scan", {"job_id": job_id})
    return user


def install(app):
    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in (
            "/api/auth/me",
            "/api/auth/start",
            "/api/auth/poll",
        ):
            return await call_next(request)

        def lookup():
            token_user = api_user(request)
            secret = request.cookies.get(COOKIE, "")
            session = (
                root_repository().load_one(
                    "sessions", hashlib.sha256(secret.encode()).hexdigest(), {}
                )
                if secret and not token_user
                else {}
            )
            user = token_user or root_repository().load_one(
                "accounts", str(session.get("user_id", ""))
            )
            return token_user, session, user

        from starlette.concurrency import run_in_threadpool

        try:
            token_user, session, user = await run_in_threadpool(lookup)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if not user or user.get("disabled") or session.get("expires", 0) < time.time():
            if token_user:
                user = token_user
            else:
                if path == "/api/health":
                    return JSONResponse({"status": "ok"})
                return JSONResponse({"detail": "Sign in with Plex to continue."}, status_code=401)
        if not token_user and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(
                request.headers.get("X-Bridge-CSRF", ""), session.get("csrf", "") or "invalid"
            ):
                return JSONResponse(
                    {"detail": "Your session changed. Refresh and try again."}, status_code=403
                )
        request.state.auth_method = "api_key" if token_user else "session"
        try:
            await run_in_threadpool(refresh_access, user, bool(token_user))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        with as_user(user):
            return await call_next(request)

    @app.get("/api/auth/me")
    def me(request: Request):
        secret = request.cookies.get(COOKIE, "")
        session = (
            root_repository().load_one("sessions", hashlib.sha256(secret.encode()).hexdigest(), {})
            if secret
            else {}
        )
        user = root_repository().load_one("accounts", str(session.get("user_id", "")))
        if not user or user.get("disabled") or session.get("expires", 0) < time.time():
            from .storage import read_json

            configured = bool(
                read_json(root_repository().directory / "config.json")
                .get("plex", {})
                .get("music_library_key")
            )
            from .login_security import setup_required

            return {
                "user": None,
                "setup_required": not configured,
                "setup_key_required": setup_required(root_repository()),
                "has_accounts": bool(users()),
            }
        return {"user": public(user), "csrf": session["csrf"]}

    class LoginStart(BaseModel):
        setup_key: str = Field(default="", max_length=200)

    @app.post("/api/auth/start")
    def start(request: Request, body: LoginStart | None = None):
        # Origin checks prevent another website initiating a login in this browser.
        origin = request.headers.get("origin")
        from urllib.parse import urlsplit

        if origin and (
            urlsplit(origin).netloc != request.headers.get("host")
            or urlsplit(origin).scheme not in ("http", "https")
        ):
            raise HTTPException(403, "Open sign-in from Harmonia.")
        from .login_security import admit_start, authorize_setup

        repo = root_repository()
        admit_start(repo, str(request.client.host if request.client else "unknown"))
        setup_hash = authorize_setup(repo, body.setup_key if body else "")
        nonce = secrets.token_urlsafe(32)
        pin = plex_json(
            "POST", "https://plex.tv/api/v2/pins", headers=headers(), data={"strong": "true"}
        )
        put(
            "login_pins",
            hashlib.sha256(nonce.encode()).hexdigest(),
            {
                "id": pin["id"],
                "code": pin["code"],
                "expires": time.time() + 600,
                "setup_hash": setup_hash,
            },
        )
        response = JSONResponse(
            {
                "url": "https://app.plex.tv/auth#?"
                + urlencode(
                    {
                        "clientID": CLIENT,
                        "code": pin["code"],
                        "context[device][product]": "Harmonia",
                    }
                )
            }
        )
        response.set_cookie(
            PIN_COOKIE,
            nonce,
            max_age=600,
            httponly=True,
            samesite="strict",
            secure=secure_cookie(request),
        )
        return response

    class LibraryChoice(BaseModel):
        library_key: str = Field(min_length=1, max_length=64, pattern=r"^\d+$")

    @app.get("/api/auth/poll")
    @app.post("/api/auth/poll")
    def poll(request: Request, body: LibraryChoice | None = None):
        if request.method == "POST" and request.headers.get("origin") != str(
            request.base_url
        ).rstrip("/"):
            raise HTTPException(403, "Choose your library from Harmonia.")
        nonce = request.cookies.get(PIN_COOKIE, "")
        key = hashlib.sha256(nonce.encode()).hexdigest()
        from .login_security import claim_poll, release_poll

        if not nonce:
            raise HTTPException(410, "Sign-in expired. Start again.")
        repo = root_repository()
        saved = claim_poll(repo, key, str(request.client.host if request.client else "unknown"))
        if saved is None:
            return JSONResponse({"pending": True}, headers={"Retry-After": "2"})
        try:
            pin = plex_json(
                "GET",
                f"https://plex.tv/api/v2/pins/{saved['id']}",
                headers=headers(),
                params={"code": saved["code"]},
            )
            if not pin.get("authToken"):
                return JSONResponse({"pending": True}, headers={"Retry-After": "2"})
            user = verify(
                pin["authToken"],
                setup_hash=saved.get("setup_hash"),
                choose_library=True,
                library_key=body.library_key if body else None,
            )
            if "setup_libraries" in user:
                return JSONResponse(user)
            delete("login_pins", key)
        finally:
            release_poll(repo, key, saved["lease"])
        secret, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        put(
            "sessions",
            hashlib.sha256(secret.encode()).hexdigest(),
            {"user_id": user["id"], "csrf": csrf, "expires": time.time() + 7 * 86400},
        )
        response = JSONResponse({"user": public(user), "csrf": csrf})
        response.set_cookie(
            COOKIE,
            secret,
            max_age=7 * 86400,
            httponly=True,
            samesite="lax",
            secure=secure_cookie(request),
        )
        response.delete_cookie(PIN_COOKIE)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request):
        delete("sessions", hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest())
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/account/initial-library")
    def initial_library():
        if not is_owner(actor()):
            return {"job": None}
        from .jobs import Store

        saved = root_repository().load_one("installation", "initial_scan", {})
        store = Store(root_repository())
        job = store.get(saved.get("job_id", ""))
        if job and job["status"] in ("failed", "cancelled", "interrupted"):
            retries = store._rows(
                "SELECT * FROM jobs WHERE action='plex_scan' AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (job["created_at"],),
            )
            if retries:
                job = retries[0]
        return {
            "job": {k: job.get(k) for k in ("id", "status", "progress", "error")} if job else None
        }

    @app.post("/api/account/manual-matches/remove")
    def remove_saved_manual_match(request: RemoveManualMatch):
        from .manual_matches import remove_manual_match

        return remove_manual_match(request)

    @app.get("/api/account/manual-matches")
    def manual_matches(
        query: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
    ):
        from .config import Config
        from .manual_matches import saved_manual_matches

        return saved_manual_matches(
            Config(read_only=True, namespaces=["mapping", "match_metadata"]), query, page, page_size
        )

    @app.get("/api/account/api-key")
    def get_api_key():
        return api_key_status(actor())

    @app.post("/api/account/api-key")
    def ensure_api_key():
        return create_api_key(actor(), False)

    @app.post("/api/account/api-key/regenerate")
    def regenerate_api_key():
        return create_api_key(actor(), True)

    @app.delete("/api/account/api-key")
    def remove_api_key():
        revoke_api_key(actor())
        return {"configured": False}

    class KeyPermissions(BaseModel):
        scopes: list[str] = Field(default_factory=lambda: ["read"], max_length=4)

    @app.put("/api/account/api-key/scopes")
    def update_api_key_scopes(body: KeyPermissions):
        return set_api_key_scopes(actor(), body.scopes)

    @app.get("/api/users")
    def listing():
        return [public(u) for u in users().values()]

    class Permissions(BaseModel):
        disabled: bool
        can_request: bool
        can_view_all_requests: bool = False
        can_playlists: bool = True
        can_library_issues: bool | None = None
        admin: bool | None = None
        can_downloads: bool = False
        can_manage_downloads: bool = False
        auto_approve: bool = False

    @app.put("/api/users/{user_id}")
    def update(user_id: str, body: Permissions):
        user = users().get(user_id)
        if not user:
            raise HTTPException(404, "User not found")
        values = body.model_dump(exclude_none=True)
        if is_owner(user) and (body.disabled or values.get("admin") is False):
            raise HTTPException(409, "The Plex server owner must remain an active administrator.")
        if (
            user.get("admin")
            and (body.disabled or values.get("admin") is False)
            and not any(
                u["id"] != user_id and u.get("admin") and not u.get("disabled")
                for u in users().values()
            )
        ):
            raise HTTPException(409, "At least one active administrator is required.")
        user["owner"] = is_owner(user)
        user.update(values)
        put("accounts", user_id, user)
        root_repository().add_log(
            "INFO",
            "Account permissions",
            f"Account {user_id}: disabled={user['disabled']}; requests={user['can_request']}",
        )
        return public(user)


def owner_key():
    return str(actor()["id"]) if actor() else "server"


def member_stores():
    from .jobs import Store

    for user in users().values():
        if not is_owner(user) and not user.get("disabled") and user.get("plex_token"):
            with as_user(user):
                yield user, Store(personal_repository())


def scheduled_members(action, schedule_id):
    if action not in ("sync", "health", "lastfm_refresh"):
        return 0
    from .config import Config
    from .sync_policy import eligible

    count = 0
    for user, store in member_stores():
        with as_user(user):
            if any(
                j["action"] == action and j["status"] in ("queued", "running", "cancelling")
                for j in store.list()
            ):
                continue
            if action == "lastfm_refresh":
                from .user_preferences import preferences

                if preferences().get("lastfm_username", "").strip():
                    store.enqueue(action, {}, schedule_id)
                    count += 1
                continue
            playlists = Config(read_only=True, namespaces=[]).config.get("playlists", [])
            if action == "sync":
                playlists = eligible(store.repository, playlists)
            if playlists:
                store.enqueue(
                    action,
                    {
                        "scope": "selected",
                        "playlist_keys": [f"{p['source']}:{p['source_id']}" for p in playlists],
                    },
                    schedule_id,
                )
                count += 1
    return count


def refresh_access(user, key_auth=False):
    """Recheck actual music-library access periodically, failing closed on errors."""
    if is_owner(user) or time.time() - user.get("verified_at", 0) < 300:
        return
    from .storage import read_json

    settings = read_json(root_repository().directory / "config.json").get("plex", {})
    libraries = plex_json(
        "GET",
        settings.get("url", "").rstrip("/") + "/library/sections",
        headers=headers(user["plex_token"]),
    )
    if not any(
        str(s.get("key")) == str(settings.get("music_library_key")) and s.get("type") == "artist"
        for s in libraries.get("MediaContainer", {}).get("Directory", [])
    ):
        raise HTTPException(403, "Plex access to this music library has been removed.")
    with root_repository().connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = _key_account(db, user)
        if key_auth and current.get("api_key_hash") != user.get("api_key_hash"):
            raise HTTPException(401, "API key changed during authorization.")
        current["verified_at"] = time.time()
        db.execute(
            "UPDATE state SET value=json_set(value,'$.verified_at',?) WHERE namespace='accounts' AND key=?",
            (current["verified_at"], str(user["id"])),
        )
        user.clear()
        user.update(current)
