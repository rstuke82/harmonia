"""Integer similarity scores with explicit, stable token preprocessing."""

import re
from functools import lru_cache
from rapidfuzz import fuzz as _fuzz


@lru_cache(maxsize=131072)
def _tokens(value):
    # Preserve the previous matcher's force_ascii and Unicode word handling.
    value = str(value).translate({i: None for i in range(128, 256)})
    return re.sub(r"(?ui)\W", " ", value).lower().strip()


def ratio(left, right):
    return int(round(_fuzz.ratio(left, right)))


@lru_cache(maxsize=131072)
def sorted_tokens(value):
    return " ".join(sorted(_tokens(value).split()))


def token_sort_ratio(left, right):
    return ratio(sorted_tokens(str(left)), sorted_tokens(str(right)))


def token_set_ratio(left, right):
    left, right = _tokens(left), _tokens(right)
    if not left or not right:
        return 0
    return int(round(_fuzz.token_set_ratio(left, right)))
