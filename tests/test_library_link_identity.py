import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from fastapi import FastAPI, HTTPException
import requests
from harmonia import accounts, discover, musicbrainz_settings
from harmonia.storage import Repository
from harmonia.plex_client import PlexAPI


class Build79Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name))
        p = patch.object(accounts, "root_repository", return_value=self.repo)
        p.start()
        self.addCleanup(p.stop)

    def test_sibling_added_between_load_and_save_survives(self):
        self.repo.put("example", "old", 1)
        stale = self.repo.load("example")
        self.repo.put("example", "new", 2)
        stale["old"] = 3
        self.repo.save({"example": stale})
        self.assertEqual(self.repo.load("example"), {"old": 3, "new": 2})
        self.repo.save({"example": {}})
        self.assertEqual(len(self.repo.load("example")), 2)
        self.repo.save({"example": {}}, replace=True)
        self.assertEqual(self.repo.load("example"), {})

    def test_baseline_deletes_only_intended_keys_and_rejects_conflicts(self):
        self.repo.save({"example": {"delete": 1, "edit": 1}})
        baseline = {"example": self.repo.load("example")}
        self.repo.put("example", "new", 2)
        self.repo.save({"example": {"edit": 1}}, baseline, replace=True)
        self.assertEqual(self.repo.load("example"), {"edit": 1, "new": 2})
        self.repo.put("example", "edit", 3)
        with self.assertRaisesRegex(RuntimeError, "Concurrent state change"):
            self.repo.save({"example": {"edit": 4, "other": 1}}, baseline, replace=True)
        self.assertEqual(self.repo.load("example"), {"edit": 3, "new": 2})

    def token(self):
        token = "pb_live_test"
        user = {
            "id": "1",
            "api_key_hash": hashlib.sha256(token.encode()).hexdigest(),
            "disabled": False,
            "can_request": False,
            "api_key_last_used_at": 0,
        }
        self.repo.put("accounts", "1", user)
        return SimpleNamespace(headers={"authorization": "Bearer " + token}), user

    def test_auth_uses_current_disabled_permissions_and_key(self):
        request, stale = self.token()
        self.repo.put("accounts", "1", {**stale, "disabled": True})
        with (
            patch.object(accounts, "users", return_value={"1": stale}),
            self.assertRaises(HTTPException) as error,
        ):
            accounts.api_user(request)
        self.assertEqual(error.exception.status_code, 403)
        self.assertTrue(self.repo.load_one("accounts", "1")["disabled"])
        self.repo.put("accounts", "1", {**stale, "can_request": True})
        user = accounts.api_user(request)
        self.assertTrue(user["can_request"])
        self.assertGreater(user["api_key_last_used_at"], 0)
        self.assertTrue(self.repo.load_one("accounts", "1")["can_request"])
        self.repo.put("accounts", "1", {**stale, "api_key_hash": "regenerated"})
        with self.assertRaises(HTTPException) as error:
            accounts.api_user(request)
        self.assertEqual(error.exception.status_code, 401)

    def endpoint(self, search, lookup=None):
        with (
            patch("harmonia.lidarr.repository", return_value=self.repo),
            patch("harmonia.lidarr.musicbrainz_search", search),
            patch("harmonia.lidarr.config", return_value={}),
        ):
            app = FastAPI()
            musicbrainz_settings.register(app)
        return next(
            r.endpoint
            for r in app.routes
            if getattr(r, "path", "") == "/api/settings/musicbrainz/test"
        )

    def test_musicbrainz_connection_errors_and_success(self):
        empty = self.endpoint(Mock(return_value={"rows": []}))
        with self.assertRaises(HTTPException) as error:
            empty()
        self.assertEqual(error.exception.status_code, 502)
        endpoint = self.endpoint(Mock(return_value={"rows": [{"album_id": "album"}]}))
        errors = [
            requests.HTTPError(response=SimpleNamespace(status_code=404)),
            requests.Timeout("unavailable"),
        ]
        for failure in errors:
            with (
                patch("harmonia.musicbrainz_client.get", side_effect=failure),
                self.assertRaises(HTTPException) as error,
            ):
                endpoint()
            self.assertEqual(error.exception.status_code, 502)
        failure = HTTPException(503, "existing")
        with (
            patch("harmonia.musicbrainz_client.get", side_effect=failure),
            self.assertRaises(HTTPException) as error,
        ):
            endpoint()
        self.assertIs(error.exception, failure)
        with patch("harmonia.musicbrainz_client.get", return_value={"title": "Album"}):
            self.assertIn("succeeded", endpoint()["message"])

    def test_dammit_keeps_correct_artist_and_punctuation(self):
        tracks = [{"title": title} for title in ("One", "Two", "Three")]
        albums = [
            {"title": "Dammit", "artist": "Ballyhoo!", "tracks": [{"title": "Wrong"}]},
            {"title": "Dammit!", "artist": "311", "tracks": tracks},
        ]
        result = discover._album_link_candidates(
            albums, artist="311", album="Dammit", source_tracks=["One", "Two", "Three"]
        )
        self.assertEqual(result[0]["artist"], "311")
        self.assertEqual(result[0]["score"], 100)
        wrong = next(row for row in result if row["artist"] == "Ballyhoo!")
        self.assertFalse(wrong["accepted"])
        self.assertLess(wrong["score"], 100)
        self.assertEqual(
            len(discover._album_link_candidates(albums, query="311", artist="311", album="Dammit")),
            1,
        )
        source = {"artist": "311", "album": "Dammit", "tracks": ["One", "Two", "Three"]}
        self.assertIsNone(
            discover._best_history_match(source, [albums[1], {**albums[1], "title": "Dammit"}])
        )

    def client(self, previous):
        client = object.__new__(PlexAPI)
        client.base_url = "http://plex"
        client.headers = {"X-Plex-Token": "token"}
        client.strict_errors = False
        contents = list(previous)
        calls = []
        client.get_playlist_items = lambda _: [
            {"plex_id": v, "playlist_item_id": str(i)} for i, v in enumerate(contents)
        ]

        def clear(_):
            contents.clear()
            return True

        client.clear_playlist = clear
        client._library_uri = lambda value: "server://id/library/metadata/" + value
        return client, contents, calls

    def test_failed_batch_restores_original_and_stops(self):
        client, contents, calls = self.client(["old1", "old2"])

        def add(_, value):
            calls.append(value)
            if value == "new100":
                return False
            contents.extend(value.split(","))
            return True

        client.add_to_playlist = add
        desired = ["new" + str(i) for i in range(201)]

        # Fail the second batch; the third must never be submitted.
        def batch(_, value):
            calls.append(value)
            if value.startswith("new100,"):
                return False
            contents.extend(value.split(","))
            return True

        client.add_to_playlist = batch
        self.assertFalse(client.replace_playlist_tracks("p", desired))
        self.assertEqual(contents, ["old1", "old2"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.repo.load("playlist_write_recovery"), {})

    def test_failed_rollback_is_durable_and_recovered_before_next_sync(self):
        client, contents, calls = self.client(["old1", "old2"])
        client.add_to_playlist = lambda *_: False
        self.assertFalse(client.replace_playlist_tracks("p", ["new"]))
        self.assertEqual(
            next(iter(self.repo.load("playlist_write_recovery").values()))["previous"],
            ["old1", "old2"],
        )

        def add(_, value):
            calls.append(value)
            contents.extend(value.split(","))
            return True

        client.add_to_playlist = add
        self.assertTrue(client.replace_playlist_tracks("p", ["next"]))
        self.assertEqual(calls, ["old1,old2", "next"])
        self.assertEqual(contents, ["next"])
        self.assertEqual(self.repo.load("playlist_write_recovery"), {})

    def test_unsafe_read_never_starts_a_write(self):
        client, _, _ = self.client(["old"])
        client.get_playlist_items = Mock(side_effect=RuntimeError("unreachable"))
        client.clear_playlist = Mock()
        with self.assertRaises(RuntimeError):
            client.replace_playlist_tracks("p", ["new"])
        client.clear_playlist.assert_not_called()
        self.assertFalse(client.strict_errors)

    def test_append_retains_existing_items_and_batches_additions(self):
        client, contents, calls = self.client(["one", "two"])
        client.clear_playlist = Mock(side_effect=AssertionError("Must retain unchanged prefix"))

        def add(_, value):
            calls.append(value)
            contents.extend(value.split(","))
            return True

        client.add_to_playlist = add
        self.assertTrue(client.replace_playlist_tracks("p", ["one", "two", "three", "four"]))
        self.assertEqual(calls, ["three,four"])
        self.assertEqual(contents, ["one", "two", "three", "four"])
        client.clear_playlist.assert_not_called()

    def test_repeated_occurrences_restore_in_exact_order(self):
        client, contents, calls = self.client(["one", "one", "two"])
        client._write_playlist_tracks = Mock(side_effect=[False, True])

        def write(_, ids):
            calls.append(list(ids))
            contents[:] = ids if len(calls) > 1 else ["partial"]
            return len(calls) > 1

        client._write_playlist_tracks.side_effect = write
        self.assertFalse(client.replace_playlist_tracks("p", ["new"]))
        self.assertEqual(contents, ["one", "one", "two"])
        self.assertEqual(calls[1], ["one", "one", "two"])

    def test_playlist_read_fetches_all_pages_before_snapshot(self):
        client = object.__new__(PlexAPI)
        client.base_url = "http://plex"
        client.headers = {}
        client.strict_errors = True
        pages = [Mock(status_code=200), Mock(status_code=200)]
        pages[0].json.return_value = {
            "MediaContainer": {
                "totalSize": 2,
                "Metadata": [{"ratingKey": "one", "playlistItemID": 1}],
            }
        }
        pages[1].json.return_value = {
            "MediaContainer": {"Metadata": [{"ratingKey": "two", "playlistItemID": 2}]}
        }
        with patch("harmonia.network.get", side_effect=pages) as get:
            self.assertEqual(client._playlist_track_ids("p"), ["one", "two"])
            self.assertEqual(get.call_args.kwargs["params"]["X-Plex-Container-Start"], 1)

    def test_all_registered_backend_actions_have_friendly_labels(self):
        from harmonia.jobs import ACTIONS
        from harmonia.job_labels import NAMES, action_name

        self.assertFalse(ACTIONS - set(NAMES))
        self.assertEqual(action_name("lastfm_refresh"), "Refresh Last.fm Discovery")
        self.assertEqual(action_name("historical_unknown_job"), "Historical Unknown Job")

    def test_failed_write_restores_repeats_through_real_queue_path(self):
        previous = ["one", "one", "two"]
        client, contents, calls = self.client(previous)
        client.add_to_playlist = lambda *_: False
        response = Mock(status_code=200)
        response.json.return_value = {
            "MediaContainer": {
                "playQueueID": "queue",
                "Metadata": [{"ratingKey": v} for v in previous],
            }
        }

        def put(*args, **kwargs):
            self.assertEqual(kwargs["params"], {"playQueueID": "queue"})
            contents[:] = previous
            return Mock(status_code=200)

        with (
            patch("harmonia.network.post", return_value=response) as post,
            patch("harmonia.network.put", side_effect=put),
        ):
            self.assertFalse(client.replace_playlist_tracks("p", ["new"]))
            post.assert_called_once()
        self.assertEqual(contents, previous)
        self.assertEqual(self.repo.load("playlist_write_recovery"), {})
