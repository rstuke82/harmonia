"""Data paths and persisted schema identifiers shared by runtime modules."""

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("HARMONIA_DATA_DIR") or Path.cwd()).expanduser().resolve()


CONFIG_FILE = CONFIG_DIR / "config.json"


MAPPING_FILE = CONFIG_DIR / "mapping.json"


MISSING_FILE = CONFIG_DIR / "missing_tracks.json"


MATCH_METADATA_FILE = CONFIG_DIR / "match_metadata.json"


SOURCE_SNAPSHOTS_FILE = CONFIG_DIR / "source_snapshots.json"


IGNORED_TRACKS_FILE = CONFIG_DIR / "ignored_tracks.json"


ARTIST_ALIASES_FILE = CONFIG_DIR / "artist_aliases.json"


STATE_SCHEMA_VERSION = 2


APPLE_ARTWORK_SIZE = 3000
