"""Admin aggregation; execution always remains in the owning account store."""

from . import accounts, jobs


def stores():
    root = accounts.root_repository()
    owner = next((u for u in accounts.users().values() if accounts.is_owner(u)), None)
    yield owner, jobs.Store(root)
    for user in accounts.users().values():
        if accounts.is_owner(user):
            continue
        with accounts.as_user(user):
            yield user, jobs.Store(accounts.personal_repository())


def describe(row, user):
    return {
        **row,
        "owner_id": user["id"] if user else "server",
        "owner_name": user.get("name", "Server") if user else "Server",
    }


def listing(summary=False, background=False):
    rows = [
        describe(row, user)
        for user, store in stores()
        for row in (store.summaries(background) if summary else store.list())
    ]
    return sorted(rows, key=lambda j: j["created_at"], reverse=True)


def resolve(job_id):
    from .api import job_store

    mine = job_store()
    if mine.contains(job_id):
        return mine
    if accounts.actor() and accounts.actor().get("admin"):
        for user, store in stores():
            if store.contains(job_id):
                return store
    return mine


def history(offset=0, limit=50, owner="", worker="", sections=False):
    where, params = jobs.history_where(worker)
    from .job_summaries import projection

    columns = projection()
    rows = []
    active = []
    total = 0
    completed_where = (
        where + " AND status NOT IN ('running','cancelling','queued')" if sections else where
    )
    order = (
        "COALESCE(finished_at,started_at,created_at) DESC,created_at DESC,id ASC"
        if sections
        else "created_at DESC"
    )
    for user, store in stores():
        if owner and str(user["id"] if user else "server") != owner:
            continue
        with store.repository.connect() as db:
            total += db.execute("SELECT COUNT(*) FROM jobs" + completed_where, params).fetchone()[0]
        rows.extend(
            describe(r, user)
            for r in store._rows(
                "SELECT "
                + columns
                + " FROM jobs"
                + completed_where
                + " ORDER BY "
                + order
                + " LIMIT ?",
                (*params, offset + limit),
            )
        )
        if sections:
            active.extend(
                describe(r, user)
                for r in store._rows(
                    "SELECT "
                    + columns
                    + " FROM jobs"
                    + where
                    + " AND status IN ('running','cancelling','queued') ORDER BY created_at ASC,id ASC",
                    params,
                )
            )
    key = (
        lambda r: (r.get("finished_at") or r.get("started_at") or r["created_at"], r["created_at"])
        if sections
        else (r["created_at"],)
    )
    result = {"rows": sorted(rows, key=key, reverse=True)[offset : offset + limit], "total": total}
    if sections:
        result["active"] = active
    return result


def job(job_id, *, row=None, store=None):
    store = store or resolve(job_id)
    row = row if row is not None else store.get(job_id)
    if not row:
        return None
    for user, candidate in stores():
        if candidate.repository.directory == store.repository.directory:
            return describe(row, user)
    return row
