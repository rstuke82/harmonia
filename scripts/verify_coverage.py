"""Measure backend coverage and enforce the audited source and sync boundaries."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / ".coverage"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "run",
                f"--data-file={data}",
                "--source=harmonia",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ],
            cwd=root,
        )
        if result.returncode:
            return result.returncode
        report = Path(directory) / "coverage.json"
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", f"--data-file={data}", "-o", str(report)],
            cwd=root,
            check=True,
        )
        coverage = json.loads(report.read_text())
        checks = {
            "harmonia/spotify_source.py": 95,
            "harmonia/apple_music_source.py": 90,
            "harmonia/sync_matching.py": 75,
            "harmonia/sync_workflow.py": 65,
        }
        failed = coverage["totals"]["percent_covered"] < 73
        print(f"Backend coverage: {coverage['totals']['percent_covered']:.1f}% (minimum 73%)")
        for path, minimum in checks.items():
            actual = coverage["files"][path]["summary"]["percent_covered"]
            print(f"{path}: {actual:.1f}% (minimum {minimum}%)")
            failed |= actual < minimum
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
