"""Release regressions for account boundaries, bounded input and saved evidence."""

import asyncio
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from starlette.requests import Request
from fastapi import HTTPException
from harmonia import accounts, api, api_v1, discover, file_audit, lidarr, lidarr_requests
from harmonia.storage import Repository
import test_account_isolation
import test_file_audit


class Access(unittest.TestCase):
    setUp = test_account_isolation.MultiUser.setUp
    http = test_account_isolation.MultiUser.http

    def test_disabled_playlist_permission_blocks_ignore_restore_but_allows_details(self):
        self.a["can_playlists"] = False
        accounts.put("accounts", self.a["id"], self.a)
        for path in ["/api/ignored/restore", "/api/playlists"]:
            status, _ = asyncio.run(
                self.http(path, "POST", self.a, body={"title": "Song", "artist": "Artist"})
            )
            self.assertEqual(status, 403, path)
        with patch("harmonia.track_routes.inspect", return_value={"tracks": []}):
            status, _ = asyncio.run(
                self.http(
                    "/api/tracks/details",
                    "POST",
                    self.a,
                    body={"title": "Song", "artist": "Artist"},
                )
            )
        self.assertEqual(status, 200)

    def test_member_request_availability_uses_personal_context_and_retains_redacted_failure(self):
        self.a["can_downloads"] = True
        self.repo.save(
            {
                "lidarr_requests": {
                    "mine": {
                        "title": "Album",
                        "artist": "Artist",
                        "requesters": [{"user_id": "11"}, {"user_id": "12"}],
                    },
                    "other": {"title": "Private", "requesters": [{"user_id": "12"}]},
                }
            }
        )
        provider = next(
            r.endpoint for r in api.app.routes if getattr(r, "path", "") == "/api/lidarr/requests"
        )
        endpoint = next(
            r.endpoint for r in api.app.routes if getattr(r, "path", "") == "/api/requests-user"
        )
        raw = [
            {
                "key": "mine",
                "title": "Album",
                "artist": "Artist",
                "availability": "Available",
                "plex_available": True,
                "download_status": "Import failed",
                "download_error": "Failure OWNER_SECRET ALICE_SECRET",
                "finalize_status": "failed",
                "finalize_error": "Not found",
                "downloads": [{"error": "OWNER_SECRET", "error_detail": "ALICE_SECRET"}],
                "requesters": [{"user_id": "11"}, {"user_id": "12"}],
                "sources": [{"title": "Private playlist"}],
            },
            {"key": "other", "title": "Private"},
        ]

        def availability(rows, **kwargs):
            self.assertEqual(accounts.actor()["id"], "11")
            self.assertEqual([r["request_key"] for r in rows], ["mine"])
            return [
                {
                    "request_key": "mine",
                    "availability": "Missing",
                    "availability_detail": "Not in personal library",
                }
            ]

        with (
            accounts.as_user(self.a),
            patch.object(provider, "__wrapped__", return_value=raw),
            patch.object(discover, "availability", side_effect=availability),
        ):
            result = endpoint.__wrapped__()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertFalse(row["plex_available"])
        self.assertEqual(row["finalize_status"], "failed")
        self.assertNotIn("SECRET", row["download_error"])
        self.assertNotIn("SECRET", json.dumps(row["downloads"]))
        self.assertEqual(row["sources"], [])
        self.assertEqual(row["requesters"], [{"user_id": "11"}])


@unittest.skipUnless(os.name == "posix", "POSIX permissions")
class CredentialModes(unittest.TestCase):
    def test_default_umask_and_existing_modes_are_private_including_migration_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "data"
            directory.mkdir(mode=0o755)
            (directory / "config.json").write_text(json.dumps({"plex": {"token": "secret"}}))
            (directory / "mapping.json").write_text("{}")
            old = os.umask(0o022)
            try:
                repo = Repository(directory)
                with repo.connect() as db:
                    db.execute("INSERT INTO state VALUES('secret','key','{}')")
                    for p in directory.iterdir():
                        if p.name in [
                            "config.json",
                            "harmonia.db",
                            "harmonia.db-wal",
                            "harmonia.db-shm",
                        ] or p.name.endswith(".bak"):
                            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                os.chmod(repo.path, 0o644)
                Repository(directory)
                self.assertEqual(stat.S_IMODE(repo.path.stat().st_mode), 0o600)
                self.assertTrue(list(directory.glob("*.pre-sqlite.bak")))
            finally:
                os.umask(old)

    def test_symlinked_database_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "target"
            target.write_text("unchanged")
            directory = root / "data"
            directory.mkdir()
            (directory / "harmonia.db").symlink_to(target)
            with self.assertRaises(OSError):
                Repository(directory)
            self.assertEqual(target.read_text(), "unchanged")


class WebhookBounds(unittest.TestCase):
    def invoke(self, chunks, headers=()):
        chunks = iter(chunks)

        async def receive():
            try:
                body = next(chunks)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": body, "more_body": True}

        request = Request({"type": "http", "headers": list(headers)}, receive)
        handler = next(
            r.endpoint
            for r in api.app.routes
            if getattr(r, "path", "") == "/api/v1/webhooks/lidarr"
        )
        with (
            patch.object(api_v1, "_admin", return_value={"id": "1"}),
            patch.object(api_v1, "_queue_lidarr_event", return_value={"accepted": True}) as queue,
            patch.object(accounts, "root_repository", return_value=None),
        ):
            result = asyncio.run(handler(request))
            return result, queue

    def test_actual_stream_limit_applies_without_or_with_misleading_length(self):
        for headers in [(), [(b"content-length", b"2")]]:
            with self.assertRaises(HTTPException) as exc:
                self.invoke([b"x" * 524288, b"x" * 524289], headers)
            self.assertEqual(exc.exception.status_code, 413)

    def test_reduced_envelope_rejects_invalid_objects_and_ids(self):
        for payload in [
            {"album": []},
            {"artist": {"id": True}},
            {"album": {"id": -1}},
            {"album": {"id": 2**64}},
        ]:
            with self.assertRaises(HTTPException) as exc:
                self.invoke([json.dumps(payload).encode()])
            self.assertEqual(exc.exception.status_code, 422)
        result, queue = self.invoke(
            [b'{"eventType":"AlbumDownload",', b'"album":{"id":123},"artist":{"id":1}}']
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(queue.call_args.args[1]["album"]["id"], 123)


class Revisions(unittest.TestCase):
    def test_poll_times_and_identical_writes_do_not_rebuild_but_errors_do(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            repo.save(
                {
                    "lidarr_requests": {
                        "album": {
                            "download_status": "Downloading",
                            "download_checked_at": 1,
                            "updated_at": 2,
                        }
                    }
                }
            )

            def generation():
                with repo.connect() as db:
                    return db.execute(
                        "SELECT generation FROM state_revisions WHERE namespace='lidarr_requests'"
                    ).fetchone()[0]

            before = generation()
            lidarr_requests.save(
                repo, "album", download_status="Downloading", download_checked_at=3
            )
            self.assertEqual(generation(), before)
            self.assertEqual(repo.load("lidarr_requests")["album"]["updated_at"], 2)
            lidarr_requests.save(repo, "album", download_error="Download failed")
            self.assertGreater(generation(), before)

    def test_job_progress_does_not_invalidate_requests_but_terminal_status_does(self):
        from harmonia.jobs import Store

        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            jid = Store(repo).enqueue("album_finalize", {})

            def generation():
                with repo.connect() as db:
                    return db.execute(
                        "SELECT generation FROM state_revisions WHERE namespace='request_jobs'"
                    ).fetchone()[0]

            before = generation()
            with repo.connect() as db:
                db.execute("UPDATE jobs SET progress=? WHERE id=?", ("Still checking", jid))
            self.assertEqual(generation(), before)
            with repo.connect() as db:
                db.execute("UPDATE jobs SET status='failed',error='Not found' WHERE id=?", (jid,))
            self.assertGreater(generation(), before)


class DuplicateScope(unittest.TestCase):
    setUp = test_file_audit.Audit.setUp
    scan = test_file_audit.Audit.scan

    def test_verify_reads_only_saved_candidates_and_never_walks(self):
        (self.root / "one.flac").write_bytes(b"same")
        (self.root / "two.flac").write_bytes(b"same")
        (self.root / "three.flac").write_bytes(b"larger unrelated")
        previous = self.scan()
        with (
            patch.object(file_audit.os, "walk", side_effect=AssertionError("No traversal")),
            patch.object(file_audit, "digest_file", wraps=file_audit.digest_file) as digest,
        ):
            result = file_audit.verify_candidates(self.cfg, self.inv, previous)
        self.assertEqual(
            {c.args[0] for c in digest.call_args_list},
            {str(self.root / "one.flac"), str(self.root / "two.flac")},
        )
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["confirmed_groups"], 1)
        (self.root / "two.flac").write_bytes(b"changed")
        result = file_audit.verify_candidates(self.cfg, self.inv, result)
        self.assertEqual(result["stale_candidates"], 1)
        self.assertNotIn("Identical files", {r["issue"] for r in result["issues"]})

    def test_old_or_missing_saved_scan_requires_scan_first(self):
        for previous in [{}, {"checked_at": time.time() - 90000}]:
            with self.assertRaises(ValueError):
                file_audit.verify_candidates(self.cfg, self.inv, previous)


class SearchPages(unittest.TestCase):
    def test_recording_pages_have_distinct_cache_and_next_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder)
            body = lidarr.Lookup(
                artist="Artist", title="Song", fields_search=True, provider="musicbrainz"
            )
            get = Mock(return_value={"recordings": [{"id": "recording"}], "recording-count": 26})
            records = [
                {
                    "releases": [
                        {
                            "release-group": {
                                "id": "album",
                                "title": "Album",
                                "artist-credit": [{"name": "Artist"}],
                                "primary-type": "Album",
                            }
                        }
                    ]
                }
            ]
            with (
                patch("harmonia.musicbrainz_client.get", get),
                patch("harmonia.song_albums.containing_albums", return_value=records),
            ):
                first = lidarr.musicbrainz_search(repo, {}, body)
                second = lidarr.musicbrainz_search(
                    repo, {}, body.model_copy(update={"recording_offset": 25})
                )
                cached = lidarr.musicbrainz_search(
                    repo, {}, body.model_copy(update={"recording_offset": 25})
                )
            self.assertEqual(first["rows"][0]["album_id"], "album")
            self.assertEqual(first["next_recording_offset"], 1)
            self.assertIsNone(second["next_recording_offset"])
            self.assertTrue(cached["cached"])
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.args[2]["offset"], 25)


class SourceManifest(unittest.TestCase):
    def test_package_includes_entire_suite_and_excludes_runtime_artifacts(self):
        import importlib.util
        import zipfile

        spec = importlib.util.spec_from_file_location(
            "package_source", Path(__file__).resolve().parents[1] / "scripts/package_source.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "source"
            root.mkdir()
            (root / "tests").mkdir()
            (root / "web").mkdir()
            for name in ["test_security.py", "evidence.log", "private.db"]:
                (root / "tests" / name).write_text("content")
            (root / ".env").write_text("TOKEN=secret")
            (root / "config.json").write_text('{"token":"secret"}')
            (root / ".env.example").write_text("PORT=8173")
            destination = Path(folder) / "source.zip"
            module.package(root, destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()), {"tests/test_security.py", ".env.example"}
                )
