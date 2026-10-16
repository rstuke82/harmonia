"""Read-only file evidence for Lidarr. Never modifies ownership, files or tags."""

import hashlib
import json
import os
import stat
import time
from collections import defaultdict
from pathlib import Path
from . import jobs
from .file_tools import mapped, plex_url

AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".ape", ".wv"}


def stable_track_id(track, tags=None):
    tags = tags or {}
    return str(
        track.get("recording_id")
        or tags.get("recording_id")
        or track.get("id")
        or fingerprint([track.get("artist"), track.get("album"), track.get("title")])
    )


def upgrade_reason(tags):
    codec = str(tags.get("codec") or tags.get("format") or "").upper()
    bitrate = int(tags.get("bitrate") or 0)
    if codec in ("FLAC", "ALAC"):
        return ""
    if codec == "AAC" and bitrate >= 240000:
        return ""
    if codec == "MP3":
        return "MP3 is eligible for a lossless or iTunes Match quality upgrade."
    if codec == "AAC":
        return f"AAC bitrate is {round(bitrate / 1000) if bitrate else 'unknown'} kbps; a higher-quality replacement may be available."
    return f"{codec or 'Unknown codec'} is not FLAC, ALAC, or approximately 256 kbps AAC."


def match_review_reason(tags):
    # High-bitrate AAC is not a library defect or evidence of iTunes Match.
    return ""


def rename_previews(client, lidarr):
    """Ask Lidarr for its own read-only rename decisions once per imported artist."""
    albums = {str(a.get("id")): a for a in lidarr.get("rows", [])}
    artists = {
        albums.get(str(t.get("lidarr_album_id")), {}).get("artist_id")
        for t in lidarr.get("tracks", [])
        if t.get("file")
    }
    artists.discard(None)
    rows = []
    errors = []
    for artist in sorted(artists, key=str):
        checkpoint("Checking Lidarr rename previews")
        try:
            values = client.call("GET", "rename", params={"artistId": artist}, quiet=True)
            if not isinstance(values, list) or any(
                not isinstance(v, dict)
                or not v.get("trackFileId")
                or not v.get("existingPath")
                or not v.get("newPath")
                for v in values
            ):
                raise ValueError("Invalid Lidarr rename preview")
            rows.extend(values)
        except Exception:
            # Never interpret an API failure as a successful empty preview.
            errors.append(artist)
            jobs.output(
                f"Lidarr rename preview unavailable for artist {artist}; no naming conclusions were made for that artist."
            )
    return {"checked_at": time.time(), "rows": rows, "failed_artists": errors}


def naming_findings(cfg, lidarr):
    preview = lidarr.get("rename_preview")
    if not isinstance(preview, dict):
        return [
            {
                "issue": "Audit unavailable",
                "reason": "Run a Lidarr Library Scan to load Lidarr rename previews. Filename guesses are no longer used.",
                "files": [],
                "evidence": {"source": "lidarr_rename_preview"},
            }
        ]
    by_file = {str(t.get("file_id")): t for t in lidarr.get("tracks", []) if t.get("file_id")}
    findings = []
    for row in preview.get("rows", []):
        track = by_file.get(str(row.get("trackFileId")))
        if not track:
            continue
        current = row.get("existingPath")
        expected = row.get("newPath")
        if not current or not expected or current == expected:
            continue
        findings.append(
            {
                "issue": "Lidarr naming mismatch",
                "reason": "Lidarr proposes renaming this imported file.",
                "files": [mapped(current, config=cfg) or current],
                "title": track.get("title", ""),
                "artist": track.get("artist", ""),
                "album": track.get("album", ""),
                "album_id": track.get("album_id"),
                "lidarr_album_id": track.get("lidarr_album_id"),
                "evidence": {
                    "source": "lidarr_rename_preview",
                    "track_file_id": row["trackFileId"],
                    "existing_path": current,
                    "expected_path": expected,
                },
                "rename_checked_at": row.get("checked_at") or preview.get("checked_at"),
                "stale": time.time() - (row.get("checked_at") or preview.get("checked_at", 0))
                > 86400,
            }
        )
    if preview.get("failed_artists"):
        findings.append(
            {
                "issue": "Audit unavailable",
                "reason": "Lidarr rename preview could not be checked for "
                + str(len(preview["failed_artists"]))
                + " artists. Run a Lidarr Library Scan to retry.",
                "files": [],
                "evidence": {
                    "source": "lidarr_rename_preview",
                    "failed_artists": preview["failed_artists"],
                },
            }
        )
    return findings


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def scope(cfg, lidarr):
    return fingerprint({"mappings": cfg["mappings"], "lidarr": lidarr.get("identity")})


def signature(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def checkpoint(message=None):
    ctx = jobs.current()
    if ctx:
        ctx.checkpoint()
    if message:
        jobs.progress(message)


def digest_file(name, expected):
    # Never follow a final symlink, and discard a checksum if the file changes.
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        if signature(os.fstat(stream.fileno())) != expected:
            raise OSError("File changed before hashing; recheck it.")
        digest = hashlib.sha256()
        while True:
            checkpoint()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if signature(os.fstat(stream.fileno())) != expected:
            raise OSError("File changed during hashing; recheck it.")
    if signature(os.stat(name, follow_symlinks=False)) != expected:
        raise OSError("File changed after hashing; recheck it.")
    return digest.hexdigest()


def inspect(cfg, lidarr, plex, previous=None, verify=False):
    previous = previous or {}
    cached = previous.get("files", {})
    files = {}
    findings = []
    roots = []
    expected = defaultdict(list)

    def add(kind, reason, paths, track=None, evidence=None):
        track = track or {}
        findings.append(
            {
                "issue": kind,
                "reason": reason,
                "files": sorted(paths),
                "title": track.get("title") or (Path(paths[0]).name if paths else kind),
                "artist": track.get("artist", ""),
                "album": track.get("album", ""),
                "album_id": track.get("album_id"),
                "evidence": evidence or {},
            }
        )

    tracks = lidarr.get("tracks", [])
    from .album_identity import Albums

    albums = Albums(lidarr, plex, cfg["mappings"])
    # File evidence can link to Plex, but never determines playback availability.
    plex_paths = defaultdict(set)
    for t in plex.get("rows", []):
        for f in t.get("files", []):
            plex_paths[f].add(str(t["plex_id"]))
    inventory_fresh = "tracks" in lidarr and time.time() - lidarr.get("checked_at", 0) < 86400
    for t in tracks:
        path = mapped(t.get("file", ""), config=cfg)
        if path:
            expected[path].append(t)
    configured = sorted({m["local_path"] for m in cfg["mappings"] if m["local_path"]})
    # Do not walk nested roots twice.
    configured = [
        r
        for r in configured
        if not any(r.startswith(parent + "/") for parent in configured if parent != r)
    ]
    scanned = 0
    for root_index, root in enumerate(configured, 1):
        checkpoint("Auditing folder: " + root)
        jobs.output(f"Scan Local Files · folder {root_index} of {len(configured)} · {root}")
        errors = []
        seen = set()
        unreadable = set()
        try:
            p = Path(root)
            if p.is_symlink() or p.resolve() != p.absolute():
                raise OSError("Symlinked audit roots are not followed.")
            if not p.is_dir():
                raise OSError("Mapped folder is unavailable. Check the read-only mount.")

            def walk_error(exc):
                errors.append(str(exc))

            for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
                checkpoint()
                for name in list(dirs):
                    if (Path(directory) / name).is_symlink():
                        dirs.remove(name)
                        errors.append("Symlinked subfolder skipped: " + str(Path(directory) / name))
                for name in names:
                    path = str(Path(directory) / name)
                    if Path(name).suffix.lower() not in AUDIO:
                        continue
                    scanned += 1
                    if scanned == 1 or scanned % 100 == 0:
                        jobs.progress(f"Scan Local Files · {scanned} audio files checked")
                    try:
                        st = os.stat(path, follow_symlinks=False)
                        if not stat.S_ISREG(st.st_mode):
                            unreadable.add(path)
                            add(
                                "Audit unavailable",
                                "Non-regular or symlinked audio file skipped.",
                                [path],
                            )
                            continue
                        seen.add(path)
                        sig = signature(st)
                        old = cached.get(path, {})
                        entry = {"signature": sig, "size": st.st_size}
                        if old.get("signature") == sig:
                            entry.update({k: old[k] for k in ("tags", "hash") if k in old})
                        if "tags" not in entry or "album_artist" not in entry.get("tags", {}):
                            try:
                                from .local_library import read_tags

                                tags = read_tags(Path(path))
                                entry["tags"] = {
                                    k: tags.get(k, "")
                                    for k in (
                                        "title",
                                        "artist",
                                        "album_artist",
                                        "album",
                                        "recording_id",
                                        "album_id",
                                        "format",
                                        "codec",
                                        "bitrate",
                                        "track_number",
                                        "disc_number",
                                    )
                                }
                            except Exception as exc:
                                entry["tag_error"] = type(exc).__name__
                                add(
                                    "Tag read error",
                                    "Audio metadata could not be read; the file was retained.",
                                    [path],
                                    evidence={"signature": sig},
                                )
                        files[path] = entry
                    except OSError as exc:
                        unreadable.add(path)
                        add("Audit unavailable", str(exc), [path])
            if errors:
                raise OSError("; ".join(errors[:5]))
            root_expected = [f for f in expected if f.startswith(root + "/")]
            if root_expected and not seen:
                raise OSError(
                    "No audio files found where Lidarr reports imports. Check the mount before declaring files missing."
                )
            roots.append({"path": root, "status": "checked", "files": len(seen)})
            if inventory_fresh:
                for path in root_expected:
                    if path not in seen and path not in unreadable:
                        add(
                            "Missing imported file",
                            "Mapped folder was readable, but this Lidarr imported file was absent.",
                            [path],
                            expected[path][0],
                            {"lidarr_path": expected[path][0].get("file")},
                        )
                for path in seen:
                    if path not in expected:
                        add(
                            "Unmanaged file",
                            "Audio file found without an imported-file association in the current Lidarr inventory.",
                            [path],
                            files[path].get("tags"),
                            {"signature": files[path]["signature"]},
                        )
        except OSError as exc:
            roots.append({"path": root, "status": "unavailable", "error": str(exc)})
            add(
                "Audit unavailable",
                str(exc) + " Missing-file conclusions for this folder were suppressed.",
                [root],
            )
    if not inventory_fresh:
        add(
            "Audit unavailable",
            "Run a current Lidarr Library Scan to identify missing or unmanaged files.",
            [],
        )
    present_ids = set()
    upgrade_ids = set()
    for path, entry in files.items():
        track = (expected.get(path) or [{}])[0]
        tags = entry.get("tags", {})
        stable = stable_track_id(track, tags)
        present_ids.add(stable)
        reason = upgrade_reason(tags) if tags else ""
        if reason:
            upgrade_ids.add(stable)
            add(
                "Upgrade candidate",
                reason,
                [path],
                track or tags,
                {
                    "stable_track_id": stable,
                    "codec": tags.get("codec") or tags.get("format"),
                    "bitrate": tags.get("bitrate"),
                    "status": "Available for review",
                },
            )
        else:
            reason = match_review_reason(tags) if tags else ""
            if reason:
                add(
                    "iTunes Match review",
                    reason,
                    [path],
                    track or tags,
                    {
                        "stable_track_id": stable,
                        "codec": tags.get("codec") or tags.get("format"),
                        "bitrate": tags.get("bitrate"),
                        "status": "Available for review",
                    },
                )
    # Preserve the work item if an upgrade candidate was moved out while its
    # replacement is pending. A new file with the same stable identity clears it.
    for old in previous.get("issues", []):
        if old.get("issue") not in ("Upgrade candidate", "iTunes Match review"):
            continue
        stable = str(old.get("evidence", {}).get("stable_track_id") or "")
        if stable and stable not in present_ids and stable not in upgrade_ids:
            label = (
                "upgrade candidate"
                if old.get("issue") == "Upgrade candidate"
                else "iTunes Match review file"
            )
            findings.append(
                {
                    **old,
                    "reason": f"Missing — awaiting upgraded file. The previous {label} has not yet been replaced.",
                    "evidence": {
                        **old.get("evidence", {}),
                        "status": "Missing — awaiting upgraded file",
                    },
                }
            )

    def album_key(path):
        t = (expected.get(path) or [files[path].get("tags", {})])[0]
        resolved = albums.resolve(t, [path])
        files[path]["album_identity"] = resolved
        # Missing/ambiguous identity must not make unrelated files duplicates.
        return (
            resolved["album_key"]
            if resolved["identity_status"] != "review"
            else ("unresolved", path)
        )

    by_recording = defaultdict(list)
    by_size = defaultdict(list)
    for path, entry in files.items():
        if entry["size"] > 0:
            by_size[(album_key(path), entry["size"])].append(path)
        ts = expected.get(path, [])
        tags = entry.get("tags", {})
        rid = next(
            (t.get("recording_id") for t in ts if t.get("recording_id")), tags.get("recording_id")
        )
        if rid:
            by_recording[(album_key(path), rid)].append(path)
    for paths in by_recording.values():
        if len(paths) > 1:
            add(
                "Possible alternate copies",
                "One recording in this album has multiple files. Formats or editions may intentionally differ.",
                paths,
                (expected.get(paths[0]) or [files[paths[0]].get("tags", {})])[0],
                {p: files[p]["signature"] for p in paths},
            )
    hashed = 0
    if verify:
        candidates = [paths for paths in by_size.values() if len(paths) > 1]
        total = sum(len(paths) for paths in candidates)
        for paths in candidates:
            hashes = defaultdict(list)
            for path in paths:
                checkpoint(f"Verifying duplicates · {hashed + 1}/{total}")
                entry = files[path]
                try:
                    value = entry.get("hash") or digest_file(path, entry["signature"])
                    entry["hash"] = value
                    hashes[value].append(path)
                except OSError as exc:
                    add("Audit unavailable", str(exc), [path])
                hashed += 1
            for digest, group in hashes.items():
                if len(group) > 1:
                    inodes = {(files[p]["signature"][0], files[p]["signature"][1]) for p in group}
                    kind = "Hard-linked paths" if len(inodes) == 1 else "Identical files"
                    add(
                        kind,
                        "SHA-256 hashes match. "
                        + (
                            "These paths share one physical file."
                            if len(inodes) == 1
                            else "Contents are byte-for-byte identical; review usage before removing any copy."
                        ),
                        group,
                        (expected.get(group[0]) or [files[group[0]].get("tags", {})])[0],
                        {"sha256": digest, "signatures": {p: files[p]["signature"] for p in group}},
                    )
    else:
        # Preserve verified evidence only while every involved signature is unchanged.
        for row in previous.get("issues", []):
            if (
                row["issue"] in ("Identical files", "Hard-linked paths")
                and len({album_key(p) for p in row["files"] if p in files}) == 1
                and all(
                    p in files
                    and files[p]["signature"]
                    == row.get("evidence", {}).get("signatures", {}).get(p)
                    for p in row["files"]
                )
            ):
                findings.append(row)
    findings = [albums.finding(row) for row in findings]
    for row in findings:
        candidates = set()
        for f in row["files"]:
            for m in cfg["mappings"]:
                if m["local_path"] and f.startswith(m["local_path"] + "/"):
                    candidate = (m.get("plex_path") or m["lidarr_path"]) + f[len(m["local_path"]) :]
                    candidates.update(plex_paths.get(candidate, set()))
        if len(candidates) == 1:
            row["plex_url"] = plex_url(plex, next(iter(candidates)))
    jobs.progress(
        f"Scan Local Files · finalizing {len(files)} audio files and {len(findings)} findings"
    )
    return {
        "checked_at": time.time(),
        "files": files,
        "roots": roots,
        "issues": findings,
        "verified_at": time.time() if verify else previous.get("verified_at"),
        "lidarr_checked_at": lidarr.get("checked_at"),
        "hashed_candidates": hashed,
    }


def verify_candidates(cfg, lidarr, previous):
    """Verify only saved same-album/size candidates; never traverse folders."""
    if not previous.get("checked_at") or time.time() - previous["checked_at"] > 86400:
        raise ValueError(
            "Run Scan Local Files first; saved duplicate evidence is missing or stale."
        )
    files = {p: dict(v) for p, v in previous.get("files", {}).items()}
    if any(not entry.get("album_identity") for entry in files.values()):
        raise ValueError(
            "Run Scan Local Files first to prepare album identity for duplicate verification."
        )
    groups = defaultdict(list)
    for path, entry in files.items():
        identity = entry.get("album_identity") or {}
        if (
            identity.get("album_key")
            and identity.get("identity_status") != "review"
            and entry.get("size", 0) > 0
        ):
            groups[(identity["album_key"], entry["size"])].append(path)
    candidates = [g for g in groups.values() if len(g) > 1]
    paths = {p for g in candidates for p in g}
    findings = [
        r
        for r in previous.get("issues", [])
        if r.get("issue") not in ("Identical files", "Hard-linked paths")
        or not paths.intersection(r.get("files", []))
    ]
    invalid = set()
    verified = 0
    confirmed = 0
    roots = [Path(m["local_path"]) for m in cfg["mappings"] if m["local_path"]]
    for group in candidates:
        hashes = defaultdict(list)
        for path in group:
            checkpoint(
                f"Verifying saved duplicate candidates · {verified + len(invalid) + 1}/{len(paths)}"
            )
            entry = files[path]
            try:
                p = Path(path)
                if (
                    not p.is_absolute()
                    or ".." in p.parts
                    or not any(
                        p != root and root in p.parents and root.resolve() == root.absolute()
                        for root in roots
                    )
                ):
                    raise OSError("Candidate is outside a current mapped folder.")
                if p.resolve() != p.absolute():
                    raise OSError("Symlinked duplicate candidate skipped.")
                st = os.stat(p, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or signature(st) != entry.get("signature"):
                    raise OSError("Candidate changed since Scan Local Files.")
                value = entry.get("hash") or digest_file(path, entry["signature"])
                entry["hash"] = value
                hashes[value].append(path)
                verified += 1
            except OSError as exc:
                invalid.add(path)
                findings.append(
                    {
                        "issue": "Audit unavailable",
                        "reason": str(exc) + " Run Scan Local Files to update duplicate evidence.",
                        "files": [path],
                    }
                )
        for digest, group in hashes.items():
            if len(group) < 2:
                continue
            confirmed += 1
            same = len({(files[p]["signature"][0], files[p]["signature"][1]) for p in group}) == 1
            findings.append(
                {
                    "issue": "Hard-linked paths" if same else "Identical files",
                    "reason": "SHA-256 hashes match. Review usage before removing any copy.",
                    "files": group,
                    "evidence": {
                        "sha256": digest,
                        "signatures": {p: files[p]["signature"] for p in group},
                    },
                    **files[group[0]].get("tags", {}),
                }
            )
    from .album_identity import Albums

    albums = Albums(lidarr, mappings=cfg["mappings"])
    return {
        **previous,
        "files": files,
        "issues": [albums.finding(r) for r in findings],
        "verified_at": time.time(),
        "hashed_candidates": verified,
        "candidate_count": len(paths),
        "stale_candidates": len(invalid),
        "confirmed_groups": confirmed,
    }


def execute(verify=False):
    from .accounts import root_repository
    from .inventory import snapshot
    from .file_tools import settings
    from .lidarr import state_put
    from .lidarr import config as lidarr_config
    from .lidarr_requests import server_id

    cfg = settings()
    if not cfg["audit_enabled"]:
        return {"summary": "Local file scanning is disabled. Enable it in Library Management."}
    if not any(m["local_path"] for m in cfg["mappings"]):
        raise ValueError("Configure a mapped music folder in Library Management first.")
    repo = root_repository()
    lidarr = snapshot(repo, "lidarr", server_id(lidarr_config(repo)))
    key = scope(cfg, lidarr)
    previous = repo.load_one("file_audit", key, {})
    # Plex is deliberately excluded here. It is not evidence of what exists on
    # disk, and decoding its full library while walking files caused large
    # transient memory spikes. Plex availability remains in its own inventory.
    result = (
        verify_candidates(cfg, lidarr, previous)
        if verify
        else inspect(cfg, lidarr, {}, previous, False)
    )
    checkpoint()
    state_put(repo, "file_audit", key, result)
    if verify:
        total = result["candidate_count"]
        checked = result["hashed_candidates"]
        stale = result["stale_candidates"]
        confirmed = result["confirmed_groups"]
        return {
            "summary": f"Verify Duplicates: {checked}/{total} saved candidates verified, {confirmed} identical groups, {stale} need a fresh Scan Local Files.",
            "partial_success": bool(stale),
            "candidates": total,
            "verified": checked,
            "confirmed": confirmed,
            "stale": stale,
        }
    count = len(result["issues"])
    unavailable = sum(r["status"] == "unavailable" for r in result["roots"])
    jobs.output(
        f"Scan Local Files checked {len(result['files'])} audio files; {count} findings. No files were changed."
    )
    return {
        "summary": f"Scan Local Files: {len(result['files'])} audio files, {count} findings, {unavailable} unavailable folders.",
        "partial_success": bool(unavailable),
        "issues": count,
    }


def decorate(rows, repo):
    reviewed = repo.load("file_issue_reviews")
    result = []
    for row in rows:
        value = {**row}
        key = fingerprint(
            {k: row.get(k) for k in ("issue", "files", "evidence", "album_id", "id", "plex_id")}
        )
        legacy = value.pop("_legacy_issue_id", None)
        if key not in reviewed and legacy in reviewed:
            key = legacy
        value["issue_id"] = key
        value["intentional"] = bool(reviewed.get(key, {}).get("intentional"))
        result.append(value)
    return result


def report(cfg, lidarr, repo, snapshot=None):
    if not cfg["audit_enabled"]:
        return {"enabled": False, "issues": []}
    snap = snapshot if snapshot is not None else repo.load_one("file_audit", scope(cfg, lidarr), {})
    stale = time.time() - snap.get("checked_at", 0) > 86400
    duplicates = {"Identical files", "Hard-linked paths", "Possible alternate copies"}
    from .album_identity import Albums

    albums = Albums(lidarr, mappings=cfg["mappings"])
    result = []
    for source in snap.get("issues", []):
        if source.get("issue") in ("iTunes Match review", "Lidarr naming mismatch"):
            continue
        if source.get("evidence", {}).get("source") == "lidarr_rename_preview":
            continue
        row = albums.finding(source)
        if row.get("issue") in duplicates:
            identities = []
            for path in row.get("files", []):
                entry = snap.get("files", {}).get(path, {})
                tags = entry.get("tags", {})
                hint = source.get("album_id") or entry.get("album_identity", {}).get("album_id")
                identities.append(albums.resolve(tags, [path], hint=hint))
            known = {r["album_key"] for r in identities if r["identity_status"] == "resolved"}
            keys = {r["album_key"] for r in identities}
            # Proven different albums are excluded. Incomplete legacy identity
            # stays visible with its hash evidence and a clear rescan request.
            if len(known) > 1:
                continue
            if (
                identities
                and len(keys) > 1
                and all(
                    snap.get("files", {}).get(p, {}).get("tags", {}).get("album_artist")
                    for p in row.get("files", [])
                )
                and not known
            ):
                continue
            if (
                identities
                and len(keys) == 1
                and all(r["identity_status"] != "review" for r in identities)
            ):
                row.update(identities[0])
            elif known and all(r["identity_status"] == "resolved" for r in identities):
                continue
            else:
                row.update(
                    identity_status="review",
                    identity_review=True,
                    original_issue=source["issue"],
                    issue="Album identity needs review",
                )
                row["reason"] = (
                    source.get("reason", "")
                    + " Album identity needs review; run Scan Local Files to refresh album-artist evidence."
                )
        result.append({**row, "audit_checked_at": snap.get("checked_at"), "stale": stale})
    result.extend(albums.finding(row) for row in naming_findings(cfg, lidarr))
    return {
        "enabled": True,
        "checked_at": snap.get("checked_at"),
        "verified_at": snap.get("verified_at"),
        "stale": stale,
        "roots": snap.get("roots", []),
        "issues": result,
    }
