import asyncio
import unittest
from types import SimpleNamespace
from harmonia.manual_matches import saved_manual_matches
from harmonia import accounts
import test_api_permissions


class ManualMatches(unittest.TestCase):
    def test_saved_only_filter_pagination_and_removed_playlists(self):
        cfg = SimpleNamespace(
            config={
                "playlists": [
                    {"source": "text", "source_id": "mix", "plex_playlist_name": "My mix"}
                ]
            },
            mapping={
                "text:mix": {"Song|Artist": "1", "Other|Artist": "2", "Auto|Artist": "3"},
                "text:deleted": {"Gone|Artist": "4"},
            },
            match_metadata={
                "text:mix": {
                    "Song|Artist": {
                        "provenance": "manual",
                        "matched_track": {"title": "Chosen", "album": "Album", "file": "SECRET"},
                    },
                    "Other|Artist": {"provenance": "manual"},
                    "Auto|Artist": {"provenance": "automatic"},
                },
                "text:deleted": {"Gone|Artist": {"provenance": "manual"}},
            },
        )
        result = saved_manual_matches(cfg, page=1, page_size=1)
        self.assertEqual((result["total"], result["pages"]), (2, 2))
        result = saved_manual_matches(cfg, "Album")
        self.assertEqual(result["total"], 1)
        self.assertNotIn("file", result["rows"][0]["match"])
        self.assertEqual(saved_manual_matches(cfg, page=999, page_size=1)["page"], 2)


class PersonalManualMatches(unittest.TestCase):
    setUp = test_api_permissions.Beta11API.setUp
    http = test_api_permissions.Beta11API.http

    def test_owner_and_member_read_only_their_own_saved_selections(self):
        for user, title in [(self.admin, "Owner song"), (self.member, "Member song")]:
            with accounts.as_user(user):
                repo = accounts.personal_repository()
                repo.put(
                    "runtime",
                    "playlists",
                    [{"source": "text", "source_id": "mix", "plex_playlist_name": "Mix"}],
                )
                repo.put("mapping", "text:mix", {title + "|Artist": "123"})
                repo.put(
                    "match_metadata", "text:mix", {title + "|Artist": {"provenance": "manual"}}
                )
        for user, title in [(self.admin, "Owner song"), (self.member, "Member song")]:
            status, result = asyncio.run(self.http("/api/account/manual-matches", user=user))
            self.assertEqual(status, 200)
            self.assertEqual([r["source"]["title"] for r in result["rows"]], [title])
        self.assertEqual(asyncio.run(self.http("/api/account/manual-matches"))[0], 401)

    def seed(self, user):
        with accounts.as_user(user):
            repo = accounts.personal_repository()
            repo.put(
                "runtime",
                "playlists",
                [{"source": "text", "source_id": "mix", "plex_playlist_name": "Mix"}],
            )
            repo.put("mapping", "text:mix", {"Song|Artist": "123", "Other|Artist": "999"})
            repo.put(
                "match_metadata",
                "text:mix",
                {
                    "Song|Artist": {"provenance": "manual", "updated_at": "stamp"},
                    "Other|Artist": {"provenance": "manual"},
                },
            )
        return dict(
            playlist_key="text:mix", search_key="Song|Artist", plex_id="123", updated_at="stamp"
        )

    def test_remove_clears_mapping_and_provenance_only_for_selected_account(self):
        from harmonia.matching import Matcher

        body = self.seed(self.member)
        self.seed(self.admin)
        status, result = asyncio.run(
            self.http(
                "/api/account/manual-matches/remove", method="POST", user=self.member, body=body
            )
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["retry_on_next_sync"])
        with accounts.as_user(self.member):
            repo = accounts.personal_repository()
            mapping = repo.load_one("mapping", "text:mix")
            self.assertEqual(mapping, {"Other|Artist": "999"})
            self.assertNotIn("Song|Artist", repo.load_one("match_metadata", "text:mix"))
            self.assertTrue(repo.load_one("runtime", "playlists")[0]["ready_to_sync"])
            # An empty cache entry now permits normal automatic evaluation.
            source = dict(title="Song", artist="Artist", album="Album", duration=240000)
            candidate = {**source, "plex_id": "auto-choice"}
            self.assertEqual(
                Matcher._match_catalog_track(source, [candidate], mapping), "auto-choice"
            )
        with accounts.as_user(self.admin):
            self.assertIn(
                "Song|Artist", accounts.personal_repository().load_one("mapping", "text:mix")
            )

    def test_stale_and_queued_selections_are_preserved(self):
        from harmonia.match_queue import stage_changes

        body = self.seed(self.member)
        status, _ = asyncio.run(
            self.http(
                "/api/account/manual-matches/remove",
                method="POST",
                user=self.member,
                body={**body, "plex_id": "changed"},
            )
        )
        self.assertEqual(status, 409)
        with accounts.as_user(self.member):
            stage_changes(accounts.personal_repository(), [body])
        status, _ = asyncio.run(
            self.http(
                "/api/account/manual-matches/remove", method="POST", user=self.member, body=body
            )
        )
        self.assertEqual(status, 409)
        with accounts.as_user(self.member):
            self.assertIn(
                "Song|Artist", accounts.personal_repository().load_one("mapping", "text:mix")
            )

    def test_removal_requires_playlist_permissions_and_browser_session(self):
        body = self.seed(self.member)
        denied = {**self.member, "can_playlists": False}
        accounts.put("accounts", denied["id"], denied)
        status, _ = asyncio.run(
            self.http("/api/account/manual-matches/remove", method="POST", user=denied, body=body)
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            asyncio.run(self.http("/api/account/manual-matches/remove", method="POST", body=body))[
                0
            ],
            401,
        )
