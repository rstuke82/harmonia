"""Optimized matching must preserve exhaustive scores, order and recording guards."""

import random
import sqlite3
import unittest
from unittest.mock import patch

from harmonia import candidate_ranking as ranking
from harmonia.matching import Matcher
from harmonia.match_candidates import consolidate
from harmonia.storage import _TimedConnection


def exhaustive(source, tracks, limit):
    rows = [ranking._row(track, Matcher.score_candidate(source, track)) for track in tracks]
    rows.sort(
        key=lambda r: (
            r["score"],
            r["identity_score"],
            r["title_score"],
            r["artist_score"],
            r["album_score"] or 0,
        ),
        reverse=True,
    )
    return consolidate(rows)[:limit]


class BoundedMatching(unittest.TestCase):
    def corpus(self):
        rng = random.Random(106)
        titles = [
            "Records",
            "We Are Young",
            "Separate Ways (Worlds Apart)",
            "Sun and Shine",
            "3 A.M.",
            "Song (Part 1)",
            "Song (Part 2)",
            "Austin",
            "Café",
        ]
        artists = ["Weezer", "Fun.", "Daughtry", "HIRIE", "Other", "The Example"]
        albums = [
            "Some Nights",
            "Sun and Shine",
            "Greatest Hits",
            "Live at Wembley",
            "A Deluxe Edition",
            "Archive Rarities",
        ]
        qualifiers = [
            "",
            " (feat. Guest)",
            " (feat. Different Guest)",
            " (Live)",
            " (Radio Edit)",
            " (Taylor’s Version)",
            " (acoustic)",
            " - remastered 2011",
            " (Boots Stop Workin')",
        ]
        rows = [
            dict(
                plex_id=str(i),
                title=rng.choice(titles) + rng.choice(qualifiers),
                artist=rng.choice(artists),
                album=rng.choice(albums),
                duration=rng.choice([None, 200000, 201000, 240000]),
                track_number=i % 20,
                disc_number=1,
            )
            for i in range(400)
        ]
        rows += [
            {**row, "plex_id": "copy:" + row["plex_id"], "files": ["/music/" + row["plex_id"]]}
            for row in rows[:40]
        ]
        for row in rows[:40]:
            row["files"] = ["/music/" + row["plex_id"]]
        return rows

    def test_shortlist_includes_exactly_the_original_strong_title_pool(self):
        rows = self.corpus() + [
            dict(title=""),
            dict(title="3 am"),
            dict(title="Café — live"),
            dict(title="Song (Part 3)"),
        ]
        for title in [
            "",
            "Records (feat. Guest)",
            "Austin (Boots Stop Workin')",
            "Song (Part 1)",
            "3 am",
            "Café",
            "Unknown title",
        ]:
            expected = [
                i
                for i, row in enumerate(rows)
                if Matcher._title_score(title, row["title"])[0] >= 95
            ]
            self.assertEqual(Matcher._strong_title_indices(title, rows), expected, title)

    def test_rank_matches_exhaustive_results_including_duplicates_and_ties(self):
        rows = self.corpus()
        sources = [
            rows[3],
            rows[20],
            dict(
                title="Records (feat. Guest)", artist="Weezer", album="Some Nights", duration=200000
            ),
            dict(
                title="Sun and Shine (feat. Eric Rachmany)",
                artist="HIRIE",
                album="Sun and Shine - Single",
                duration=204000,
            ),
            dict(title="Unknown", artist="Unknown", album="Unknown"),
        ]
        for source in sources:
            for limit in [1, 5, 20, 100]:
                with self.subTest(source=source["title"], limit=limit):
                    actual, _ = ranking.rank(source, rows, limit)
                    self.assertEqual(actual, exhaustive(source, rows, limit))

    def test_upper_bound_covers_structured_credit_boosts_and_penalties(self):
        rows = self.corpus()
        sources = [
            dict(title="Records", artist="Weezer", album="Some Nights", duration=200000),
            dict(
                title="Records (feat. Guest)",
                artist="Weezer, Guest",
                artists=[{"name": "Weezer"}, {"name": "Guest"}],
                album="Some Nights",
                duration=200000,
            ),
        ]
        for source in sources:
            for row in rows:
                score = Matcher.score_candidate(source, row)["adjusted_score"]
                self.assertGreaterEqual(ranking.upper_bound(source, row), round(score, 1))

    def test_pruning_reduces_full_scoring_without_changing_top_results(self):
        source = dict(
            title="We Are Young (feat. Janelle Monáe)",
            artist="Fun.",
            album="Some Nights",
            duration=251000,
        )
        rows = [
            dict(
                plex_id=str(i),
                title=f"Unrelated piece {i}",
                artist="Other",
                album="Other",
                duration=200000,
            )
            for i in range(1000)
        ]
        rows += [
            dict(
                plex_id=f"wanted:{i}",
                title="We Are Young",
                artist="Fun",
                album="Some Nights",
                duration=251000,
            )
            for i in range(25)
        ]
        expected = exhaustive(source, rows, 20)
        with patch.object(Matcher, "score_candidate", wraps=Matcher.score_candidate) as scorer:
            actual, count = ranking.rank(source, rows, 20)
        self.assertEqual(actual, expected)
        self.assertEqual(count, scorer.call_count)
        self.assertLess(count, 100)

    def test_cached_artist_text_does_not_cache_account_alias_decisions(self):
        token = Matcher._alias_context.set({"first": ["second"]})
        try:
            self.assertEqual(Matcher._artist_score("first", "second"), 100)
            Matcher._alias_context.set({})
            self.assertLess(Matcher._artist_score("first", "second"), 100)
        finally:
            Matcher._alias_context.reset(token)


class DatabaseTiming(unittest.TestCase):
    def test_slow_statement_diagnostics_do_not_expose_sql_or_parameters(self):
        db = sqlite3.connect(":memory:", factory=_TimedConnection)
        try:
            with (
                patch("harmonia.storage.time.monotonic", side_effect=[1.0, 2.0]),
                patch("harmonia.storage.time.thread_time", side_effect=[1.0, 1.1]),
                self.assertLogs("uvicorn.error", level="INFO") as logs,
            ):
                self.assertEqual(
                    db.execute("SELECT ? AS secret_alias", ("private-key",)).fetchone()[0],
                    "private-key",
                )
            text = "\n".join(logs.output)
            self.assertIn("SELECT execute 1.00s", text)
            self.assertNotIn("private-key", text)
            self.assertNotIn("secret_alias", text)
        finally:
            db.close()

    def test_timing_connection_retains_transaction_rollback(self):
        db = sqlite3.connect(":memory:", factory=_TimedConnection)
        try:
            db.execute("CREATE TABLE test(value)")
            with self.assertRaises(ValueError), db:
                db.execute("INSERT INTO test VALUES (?)", ("rollback",))
                raise ValueError("stop")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM test").fetchone()[0], 0)
        finally:
            db.close()
