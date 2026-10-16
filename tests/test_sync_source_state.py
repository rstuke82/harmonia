"""Source identity repair preserves account state and duplicate occurrence changes."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from harmonia.sync_engine import Syncer


class SourceState(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            mapping={"spotify:old": {"Song|Artist": "1"}},
            missing={"spotify:old": [{"title": "Song"}]},
            match_metadata={"spotify:old": {"Song|Artist": {"provenance": "manual"}}},
            source_snapshots={"spotify:old": [{"key": "id:1"}]},
            ignored_tracks={"spotify:old": {"ignored": True}},
            save=Mock(),
        )
        self.sync = Syncer(self.cfg)
        self.entry = {"source_id": "old", "source_url": "old-url"}

    def test_source_identity_repair_moves_every_saved_bucket(self):
        self.sync._repair_source_entry(self.entry, "spotify", "new", "new-url", False)
        for attr in ("mapping", "missing", "match_metadata", "source_snapshots", "ignored_tracks"):
            bucket = getattr(self.cfg, attr)
            self.assertNotIn("spotify:old", bucket)
            self.assertIn("spotify:new", bucket)
        self.assertEqual(self.entry, {"source_id": "new", "source_url": "new-url"})
        self.cfg.save.assert_called_once()

    def test_repair_does_not_overwrite_newer_missing_or_snapshot(self):
        for attr in ("missing", "source_snapshots"):
            getattr(self.cfg, attr)["spotify:new"] = ["newer"]
        self.sync._repair_source_entry(self.entry, "spotify", "new", "new-url", False)
        self.assertEqual(self.cfg.missing["spotify:new"], ["newer"])
        self.assertEqual(self.cfg.source_snapshots["spotify:new"], ["newer"])

    def test_dry_run_and_unchanged_identity_do_not_save(self):
        before = copy.deepcopy(self.cfg.mapping)
        self.sync._repair_source_entry(self.entry, "spotify", "new", "new-url", True)
        self.assertEqual(self.cfg.mapping, before)
        self.sync._repair_source_entry(self.entry, "spotify", "old", "old-url", False)
        self.cfg.save.assert_not_called()

    def test_duplicate_changes_count_occurrences_and_indices(self):
        one = {"title": "One", "artist": "Artist", "source_id": "1"}
        two = {"title": "Two", "artist": "Artist", "source_id": "2"}
        self.sync._save_source_snapshot("p", [one, one, two])
        changes = self.sync._source_change_report("p", [one, two, two])
        self.assertFalse(changes["baseline"])
        self.assertEqual([v["source_id"] for v in changes["removed"]], ["1"])
        self.assertEqual([v["source_id"] for v in changes["added"]], ["2"])
        self.assertEqual(changes["added_indices"], {2})
        with patch("harmonia.sync_sources.emit") as emit:
            self.sync._print_source_changes(changes)
        self.assertTrue(emit.called)

    def test_first_observation_is_a_baseline(self):
        self.assertTrue(self.sync._source_change_report("never-observed", [])["baseline"])
        self.assertEqual(
            self.sync._source_track_snapshot_key({"title": "One", "artist": "Artist"}),
            "meta:one|artist",
        )
