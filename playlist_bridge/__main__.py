"""Harmonia web application launcher."""

import sys

from . import __release_name__, __version__


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "help"

    if command in {"web", "serve", "ui"}:
        from .api import run
        run()
        return 0

    if command in {"version", "--version", "-v"}:
        print(f"{__version__}")
        return 0

    print(__release_name__)
    print("Usage:")
    print("  python -m playlist_bridge web      # web UI/API on port 8173")
    print("  python -m playlist_bridge version  # installed version")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
