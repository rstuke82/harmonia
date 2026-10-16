import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import HTTPException
from harmonia import accounts, jobs, library
from harmonia.storage import Repository
from harmonia.plex_client import PlexAPI
from harmonia.process_lock import ProcessLock
from harmonia.album_apple_artwork import artwork


class Build83(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name))
        for name in ("root_repository", "personal_repository"):
            p = patch.object(accounts, name, return_value=self.repo)
            p.start()
            self.addCleanup(p.stop)

    def client(self):
        c = object.__new__(PlexAPI)
        c.base_url = "http://plex"
        c.headers = {}
        c.strict_errors = False
        contents = ["old"]
        calls = []
        c.get_playlist_items = lambda _: [
            {"plex_id": v, "playlist_item_id": str(i + 1)} for i, v in enumerate(contents)
        ]
        c.clear_playlist = lambda _: contents.clear() or True

        def add(_, value):
            calls.append(value)
            # HTTP success with an ineffective batch, as observed in production.
            if "," not in value:
                contents.append(value)
            return True

        c.add_to_playlist = add
        return c, contents, calls

    def test_http_success_missing_batch_retries_individually(self):
        c, contents, calls = self.client()
        with patch.object(jobs, "output") as output:
            self.assertTrue(c.replace_playlist_tracks("p", ["a", "b"]))
        self.assertEqual(contents, ["a", "b"])
        self.assertEqual(calls, ["a,b", "a", "b"])
        self.assertEqual(self.repo.load("playlist_write_recovery"), {})
        messages = [str(call.args[0]) for call in output.call_args_list]
        self.assertTrue(any("expected 2 tracks; returned 0" in m for m in messages))
        self.assertTrue(any("expected library ID a" in m for m in messages))

    def test_failed_individual_retry_restores_previous(self):
        c, contents, calls = self.client()
        add = c.add_to_playlist
        c.add_to_playlist = lambda p, v: False if v == "b" else add(p, v)
        self.assertFalse(c.replace_playlist_tracks("p", ["a", "b"]))
        self.assertEqual(contents, ["old"])
        self.assertEqual(self.repo.load("playlist_write_recovery"), {})

    def test_recovery_uses_individual_additions_when_batch_is_ineffective(self):
        c, contents, calls = self.client()
        contents.clear()
        self.assertTrue(c._restore_playlist_tracks("p", ["old1", "old2"]))
        self.assertEqual(contents, ["old1", "old2"])
        self.assertEqual(calls, ["old1,old2", "old1", "old2"])

    def test_library_search_index_does_not_share_members_tracks(self):
        from harmonia import library_inventory

        library_inventory.clear()
        self.addCleanup(library_inventory.clear)
        cfg = SimpleNamespace(
            repository=self.repo,
            config={"plex": {"url": "http://plex", "token": "token", "music_library_key": "1"}},
        )

        def snapshots(repo, config):
            user = accounts.actor()["id"]
            return {"checked_at": 1, "rows": [{"plex_id": user, "artist": user}]}, {}

        with (
            patch("harmonia.inventory.current", side_effect=snapshots),
            patch("harmonia.lidarr.config", return_value={"enabled": False, "url": ""}),
            patch("harmonia.api._config", return_value=cfg),
        ):
            with accounts.as_user({"id": "one"}):
                one = library.playable_tracks()
            with accounts.as_user({"id": "two"}):
                two = library.playable_tracks()
        self.assertEqual([t["plex_id"] for t in one], ["one"])
        self.assertEqual([t["plex_id"] for t in two], ["two"])

    def test_reordered_readback_reports_positions(self):
        c, _, _ = self.client()
        c._playlist_track_ids = lambda _: ["b", "a"]
        with patch.object(jobs, "output") as output:
            self.assertFalse(c._verify_playlist_tracks("p", ["a", "b"]))
        self.assertTrue(
            any(
                "position 1: expected library ID a; returned b" in call.args[0]
                for call in output.call_args_list
            )
        )

    def test_background_lock_waits_and_acquires_after_release(self):
        path = Path(self.temp.name) / "test.lock"
        waiting = threading.Event()
        finished = threading.Event()
        errors = []

        def worker():
            try:
                with patch.object(
                    jobs, "current", return_value=SimpleNamespace(checkpoint=waiting.set)
                ):
                    with ProcessLock(path):
                        finished.set()
            except BaseException as exc:
                errors.append(exc)

        with ProcessLock(path, wait=False):
            t = threading.Thread(target=worker)
            t.start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(finished.is_set())
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])

    def test_waiting_job_can_cancel_and_interactive_lock_fails_fast(self):
        path = Path(self.temp.name) / "test.lock"
        with ProcessLock(path, wait=False):
            with patch.object(jobs, "current", return_value=None), self.assertRaises(RuntimeError):
                with ProcessLock(path):
                    pass
            ctx = SimpleNamespace(checkpoint=Mock(side_effect=jobs.Cancelled()))
            with patch.object(jobs, "current", return_value=ctx), self.assertRaises(jobs.Cancelled):
                with ProcessLock(path):
                    pass
        with ProcessLock(path, wait=False):
            pass

    def test_matching_reads_library_index_without_full_rebuild(self):
        data = {"plex": {"checked_at": 1}, "playable_tracks": {"visible": {"plex_id": "visible"}}}
        with (
            patch("harmonia.api._config", return_value=SimpleNamespace(repository=self.repo)),
            patch.object(library, "indexed", return_value=data),
            patch.object(
                library, "build_library", side_effect=AssertionError("Full Library rebuild")
            ),
        ):
            self.assertEqual(library.playable_tracks(), [{"plex_id": "visible"}])
            data["plex"] = {}
            with self.assertRaises(HTTPException) as exc:
                library.playable_tracks()
            self.assertEqual(exc.exception.status_code, 409)

    def test_preloaded_artwork_preserves_reviewed_link_without_per_release_reads(self):
        repo = Mock()
        links = {"1": {"apple": "new"}, "2": {"apple": ""}}
        covers = {
            "1": {"url": "old", "artwork": "old-cover"},
            "2": {"url": "", "artwork": "old-cover"},
        }
        for _ in range(200):
            self.assertEqual(
                artwork(repo, "1", "automatic", saved_links=links, saved_artwork=covers), ""
            )
            self.assertEqual(
                artwork(repo, "2", "automatic", saved_links=links, saved_artwork=covers), ""
            )
            self.assertEqual(
                artwork(repo, "3", "automatic", saved_links=links, saved_artwork=covers),
                "automatic",
            )
        repo.load_one.assert_not_called()
        covers["1"] = {"url": "new", "artwork": "chosen"}
        self.assertEqual(artwork(repo, "1", saved_links=links, saved_artwork=covers), "chosen")


if __name__ == "__main__":
    unittest.main()
