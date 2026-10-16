"""Parse collaboration separators without stripping X from artist names."""

import re

from .text_utils import repair_text

# Keep x collaboration support only when surrounded by spaces and artist text.
_NEW = re.compile(
    r"\s*(?:,|&|\bfeat(?:uring)?\.?\b|\bft\.?\b|(?<=\s)x(?=\s+\S)(?!\s*(?:,|&|feat(?:uring)?\.?\b|ft\.?\b)))\s*",
    re.I,
)
_OLD = re.compile(r"\s*(?:,|&|\bfeat(?:uring)?\.?\b|\bft\.?\b|\bx\b)\s*", re.I)


def names(artist):
    return [repair_text(value).strip() for value in _NEW.split(str(artist or "")) if value.strip()]


def structured(track):
    credits = track.get("artists")
    if not isinstance(credits, list):
        return []
    current = [
        repair_text(str(value.get("name") or "") if isinstance(value, dict) else str(value)).strip()
        for value in credits
    ]
    artist = str(track.get("artist") or "")
    legacy = [repair_text(value).strip() for value in _OLD.split(artist) if value.strip()]
    corrected = names(artist)
    # Repair only the exact output of the old parser. Deliberately different
    # structured credits still carry their own identity and are not overwritten.
    if current == legacy and legacy != corrected:
        return [{"name": name} for name in corrected]
    return credits
