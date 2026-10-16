"""Integer similarity scores with explicit, stable token preprocessing."""

import re
from rapidfuzz import fuzz as _fuzz


def _tokens(value):
    # Preserve the previous matcher's force_ascii and Unicode word handling.
    value = str(value).translate({i: None for i in range(128, 256)})
    return re.sub(r"(?ui)\W", " ", value).lower().strip()


def ratio(left, right):
    return int(round(_fuzz.ratio(left, right)))


def token_sort_ratio(left, right):
    return ratio(" ".join(sorted(_tokens(left).split())), " ".join(sorted(_tokens(right).split())))


def token_set_ratio(left, right):
    left, right = _tokens(left), _tokens(right)
    if not left or not right:
        return 0
    return int(round(_fuzz.token_set_ratio(left, right)))
