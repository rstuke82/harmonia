import unittest
from unittest.mock import Mock, patch, call
from harmonia.release_change import execute


class ReadOnlyReleaseSelectionTests(unittest.TestCase):
    def test_release_selection_never_opens_media_or_reads_track_files(self):
        album = {
            "foreignAlbumId": "group",
            "artistId": 2,
            "releases": [
                {"foreignReleaseId": "chosen", "monitored": False},
                {"foreignReleaseId": "other", "monitored": True},
            ],
        }
        client = Mock()
        client.call.side_effect = [album, {}, {"id": 42}]
        with (
            patch("harmonia.accounts.root_repository"),
            patch("harmonia.lidarr.config", return_value={}),
            patch("harmonia.lidarr.Client", return_value=client),
            patch(
                "harmonia.release_change._release",
                return_value={"id": "chosen", "title": "Release", "release-group": {"id": "group"}},
            ),
            patch("harmonia.inventory.scan", return_value={"summary": "scanned"}),
            patch("mutagen.File", side_effect=AssertionError("Must not open media")) as media,
            patch(
                "harmonia.file_tools.mapped",
                side_effect=AssertionError("Must not access media paths"),
            ) as mapped,
        ):
            result = execute({"lidarr_id": 1, "release_group_id": "group", "release_id": "chosen"})
        media.assert_not_called()
        mapped.assert_not_called()
        self.assertEqual(
            client.call.call_args_list,
            [
                call("GET", "album/1"),
                call("PUT", "album/1", json=album),
                call("POST", "command", json={"name": "RefreshAlbum", "albumId": 1}),
            ],
        )
        self.assertFalse(album["anyReleaseOk"])
        self.assertTrue(album["releases"][0]["monitored"])
        self.assertFalse(album["releases"][1]["monitored"])
        self.assertNotIn("tagged", result)
        self.assertNotIn("wrote", result["summary"])

    def test_wrong_release_group_is_rejected_before_lidarr_writes(self):
        client = Mock()
        with (
            patch("harmonia.accounts.root_repository"),
            patch("harmonia.lidarr.config", return_value={}),
            patch("harmonia.lidarr.Client", return_value=client),
            patch(
                "harmonia.release_change._release",
                return_value={"id": "chosen", "release-group": {"id": "other"}},
            ),
        ):
            with self.assertRaises(ValueError):
                execute({"lidarr_id": 1, "release_group_id": "group", "release_id": "chosen"})
        client.call.assert_not_called()
