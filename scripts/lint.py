"""Fail CI on new lint findings; format checked modules without rewriting the backlog."""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "lint-baseline.json"
FORMAT_PATHS = [
    "harmonia/job_summaries.py",
    "tests/test_responsive_activity.py",
    "tests/test_page_preparation.py",
    "scripts/verify_page_loading.py",
    "harmonia/webhook_transport.py",
    "harmonia/notifications.py",
    "harmonia/network.py",
    "harmonia/diagnostics.py",
    "harmonia/updates.py",
    "scripts/lint.py",
    "harmonia/artist_credits.py",
    "tests/test_artist_credit_queue.py",
    "tests/test_request_visibility.py",
    "tests/test_network_consistency.py",
    "scripts/verify_browser_csp.py",
    "harmonia/browser_security.py",
    "harmonia/storage.py",
    "harmonia/verification.py",
    "harmonia/sync_health.py",
    "harmonia/source_observations.py",
    "harmonia/sync_projection.py",
    "tests/test_repository_connections.py",
    "tests/test_sync_outcomes.py",
    "tests/test_security_robustness.py",
    "tests/test_update_controls.py",
    "tests/test_playlist_source_fetching.py",
]


def fingerprints(findings):
    return Counter(
        (Path(row["filename"]).resolve().relative_to(ROOT).as_posix(), row["code"], row["message"])
        for row in findings
    )


def main():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "harmonia",
            "scripts",
            "tests",
            "--output-format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
        return result.returncode
    current = fingerprints(json.loads(result.stdout))
    if sys.argv[1:] == ["--write-baseline"]:
        BASELINE.write_text(
            json.dumps(
                [
                    {"path": key[0], "code": key[1], "message": key[2], "count": count}
                    for key, count in sorted(current.items())
                ],
                indent=2,
            )
            + "\n"
        )
        print(f"Recorded {sum(current.values())} existing lint findings.")
        return 0
    if sys.argv[1:]:
        print("Usage: python scripts/lint.py [--write-baseline]", file=sys.stderr)
        return 2
    baseline = Counter(
        {
            (row["path"], row["code"], row["message"]): row["count"]
            for row in json.loads(BASELINE.read_text())
        }
    )
    added = current - baseline
    for (path, code, message), count in sorted(added.items()):
        print(f"{path}: {code} {message} ({count} new)", file=sys.stderr)
    formatted = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *FORMAT_PATHS], cwd=ROOT
    )
    print(f"{sum(current.values())} existing lint findings; {sum(added.values())} new.")
    return 1 if added or formatted.returncode else 0


if __name__ == "__main__":
    raise SystemExit(main())
