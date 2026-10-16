"""Require clean Python lint and consistent formatting across the repository."""

import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    paths = ["harmonia", "scripts", "tests"]
    lint = subprocess.run([sys.executable, "-m", "ruff", "check", *paths], cwd=root)
    formatting = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", *paths], cwd=root
    )
    return int(bool(lint.returncode or formatting.returncode))


if __name__ == "__main__":
    raise SystemExit(main())
