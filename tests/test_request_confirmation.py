import unittest
from harmonia.request_confirmation import apply


class Confirmation(unittest.TestCase):
    def setUp(self):
        self.record = {
            "webhook_checked_at": 1,
            "plex_confirmation": {
                "server": "s",
                "webhook_at": 1,
                "checked_at": 10,
                "found": 12,
                "expected": 12,
            },
        }
        self.info = {"availability": "Unknown", "checked_at": 100}

    def test_unknown_scan_preserves_targeted_success(self):
        self.assertEqual(apply(self.record, self.info, "s")["availability"], "Available")

    def test_partial(self):
        self.record["plex_confirmation"]["found"] = 5
        self.assertEqual(apply(self.record, self.info, "s")["availability"], "Partially Available")

    def test_newer_definitive_inventory_wins(self):
        info = {"availability": "Unavailable", "checked_at": 20}
        self.assertEqual(apply(self.record, info, "s"), info)

    def test_server_and_new_event_invalidate(self):
        self.assertEqual(apply(self.record, self.info, "other"), self.info)
        self.record["webhook_checked_at"] = 2
        self.assertEqual(apply(self.record, self.info, "s"), self.info)

    def test_removed_failure_or_no_tracks_not_completed(self):
        for field in ["removed_from_lidarr", "webhook_failure", "download_error"]:
            self.assertEqual(apply({**self.record, field: True}, self.info, "s"), self.info)
        self.record["plex_confirmation"]["found"] = 0
        self.assertEqual(apply(self.record, self.info, "s"), self.info)
