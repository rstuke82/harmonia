"""Track scoring, artist aliases, version guards and automatic matching."""

from typing import List
from typing import Optional
from typing import Tuple
import contextvars
from . import fuzzy_scores as fuzz
from functools import lru_cache
import re
import unicodedata
from .text_utils import repair_text


class Matcher:
    """Track matching logic with title/artist identity + album preference."""

    # Normalized artist -> every normalized name in the same global alias group.
    ARTIST_ALIAS_LOOKUP = {}
    _alias_context = contextvars.ContextVar("matcher_aliases", default=None)

    MATCH_THRESHOLD = 90
    PROMPT_THRESHOLD = 70
    MIN_DISPLAY_SCORE = 50

    # These are ranking penalties, not hard exclusions. If the only copy
    # available is on a compilation/live/deluxe release, it can still match.
    ALBUM_TYPE_PENALTIES = {
        "compilation": 14,
        "live": 16,
        "remix": 14,
        "acoustic": 10,
        "demo": 16,
        "session": 14,
    }

    # Track-title markers that identify a distinct recording/version.
    # Featured-artist credits are intentionally NOT included here.
    # "session" means branded platform sessions (iTunes/Apple Music/Spotify),
    # not generic album-era provenance such as "(Evolver Sessions)".
    VERSION_TYPES = {
        "remix",
        "live",
        "acoustic",
        "demo",
        "session",
    }

    @staticmethod
    @lru_cache(maxsize=65536)
    def _normalize_match_text(value: str) -> str:
        """Normalize punctuation/whitespace used in title and artist scoring."""
        if not value:
            return ""

        text = unicodedata.normalize("NFKD", repair_text(value)).casefold()
        text = "".join(c for c in text if not unicodedata.combining(c))
        # Equivalent Unicode hyphens/dashes are common in provider album names.
        text = text.translate(str.maketrans("‐‑‒–—―−", "-------"))
        text = re.sub(r"(?<!\w)&(?!\w)", " and ", text)
        text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
        text = text.replace("'", "").replace("’", "").replace("‘", "")
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    @lru_cache(maxsize=65536)
    def _strip_title_metadata(cls, title: str) -> str:
        """
        Remove common release/credit qualifiers that are usually metadata,
        while leaving arbitrary parenthetical subtitles intact.

        Examples:
            Dark Sky (feat. S.A. Martinez) -> Dark Sky
            Song (Remastered 2011) -> Song
            Song - Radio Edit -> Song
        """
        value = cls._normalize_match_text(title)

        if not value:
            return ""

        # Parenthetical/bracketed metadata.
        metadata_parenthetical = re.compile(
            r"\s*[\(\[]\s*"
            r"(?:"
            r"feat(?:uring)?\.?|ft\.?|with|"
            r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
            r"live\b[^)\]]*|"
            r"acoustic|stripped|"
            r"demo\b[^)\]]*|"
            r"(?:itunes\s+)?sessions?\b[^)\]]*|"
            r"[^)\]]+\s+sessions?\b[^)\]]*|"
            r"radio\s+(?:edit|mix)|single\s+edit|edit|"
            r"remix(?:ed)?|mix|"
            r"[^)\]]+\s+(?:remix|mix)\b[^)\]]*|"
            r"mono|stereo|"
            r"bonus\s+track|"
            r"from\s+.+?(?:soundtrack|motion\s+picture)"
            r")"
            r"[^)\]]*[\)\]]",
            re.IGNORECASE,
        )

        value = metadata_parenthetical.sub(" ", value)

        # Suffix metadata outside parentheses.
        value = re.sub(
            r"\s*[-:]\s*"
            r"(?:"
            r"feat(?:uring)?\.?|ft\.?|with|"
            r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
            r"live(?:\s+(?:at|from|in|on)\b.*)?|"
            r"acoustic|stripped|"
            r"demo\b.*|"
            r"(?:itunes\s+)?sessions?\b.*|"
            r"radio\s+(?:edit|mix)|single\s+edit|edit|"
            r"remix(?:ed)?|mix|mono|stereo"
            r")\b.*$",
            "",
            value,
            flags=re.IGNORECASE,
        )

        # Featured-credit suffix without punctuation.
        value = re.sub(
            r"\s+(?:feat(?:uring)?\.?|ft\.?)\s+.+$",
            "",
            value,
            flags=re.IGNORECASE,
        )

        return re.sub(r"\s+", " ", value).strip(" -:")

    @classmethod
    @lru_cache(maxsize=65536)
    def _strip_trailing_parenthetical(cls, title: str) -> Tuple[str, bool]:
        """
        Return title without one arbitrary trailing (...) or [...] qualifier.

        This broad fallback is only compared when one side has the trailing
        qualifier and the other side does not. That avoids collapsing:
            Song (Part 1)
            Song (Part 2)
        into the same title.
        """
        value = cls._normalize_match_text(title)

        if not value:
            return "", False

        stripped = re.sub(
            r"\s*[\(\[][^)\]]+[\)\]]\s*$",
            "",
            value,
        ).strip()

        return stripped, stripped != value

    @classmethod
    def _taylors_version(cls, track):
        return cls._has_taylors_version(
            str(track.get("title") or ""), str(track.get("album") or "")
        )

    @classmethod
    @lru_cache(maxsize=65536)
    def _has_taylors_version(cls, title, album):
        value = cls._normalize_match_text(title + " " + album)
        return bool(re.search(r"\btaylors?\s+version\b", value))

    @classmethod
    def _recording_version_mismatch(cls, source, candidate):
        # A re-recording is a different identity, in either direction.
        if cls._taylors_version(source) != cls._taylors_version(candidate):
            return True
        return cls._feature_identity_reason(source, candidate).startswith("Conflict:")

    @classmethod
    @lru_cache(maxsize=65536)
    def _guest_names(cls, title, artist, album):
        values = []
        for raw in (title, artist, album):
            value = cls._normalize_match_text(raw)
            for credit in re.findall(
                r"(?:^|[\s(\[\-:])(?:feat(?:uring)?\.?|ft\.?)\s+([^\)\]]+)", value
            ):
                credit = re.sub(r"\s*-\s*(?:single|ep)$", "", credit).strip()
                values.extend(
                    part.strip()
                    for part in re.split(r"\s*(?:,|&|\band\b)\s*", credit)
                    if part.strip()
                )
        return tuple(values)

    @classmethod
    def _feature_identity_reason(cls, source, candidate):
        """Distinguish explicit guest conflicts from corroborated omissions."""

        def text(track):
            from .artist_credits import structured

            credits = structured(track)
            names = (
                [c.get("name", "") if isinstance(c, dict) else str(c) for c in credits]
                if isinstance(credits, list)
                else []
            )
            return cls._normalize_match_text(
                " ".join([str(track.get(k) or "") for k in ("title", "artist", "album")] + names)
            )

        def guests(track):
            return cls._guest_names(
                *(str(track.get(key) or "") for key in ("title", "artist", "album"))
            )

        source_guests, candidate_guests = guests(source), guests(candidate)
        if not source_guests and not candidate_guests:
            return ""

        def missing(required, available):
            return any(
                not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", available)
                for name in required
            )

        omitted = missing(source_guests, text(candidate)) or missing(candidate_guests, text(source))
        if not omitted:
            return "Featured artist credit agrees"
        if source_guests and candidate_guests:
            return "Conflict: different featured artists"
        left = str(source.get("recording_id") or "")
        right = str(candidate.get("recording_id") or "")
        if left and right:
            return (
                "Shared recording ID confirms omitted guest credit"
                if left == right
                else "Conflict: different recording IDs"
            )

        # Omitted credits can be corroborated by the same album and a close
        # duration. A separately named featured single cannot use this path.
        def album(track):
            return cls._album_identity(track.get("album") or "")

        def lead(track):
            from .artist_credits import structured

            credits = structured(track)
            if isinstance(credits, list) and credits:
                first = credits[0]
                value = first.get("name", "") if isinstance(first, dict) else str(first)
            else:
                value = str(track.get("artist") or "")
                value = re.split(
                    r"\s+(?:feat(?:uring)?\.?|ft\.?)\s+", value, maxsplit=1, flags=re.I
                )[0]
                if source_guests or candidate_guests:
                    value = value.split(",", 1)[0]
            return re.sub(r"[^\w]+", " ", cls._normalize_match_text(value)).strip()

        from .track_duration import source_duration

        def seconds(track):
            value = source_duration(track)
            return value / 1000 if value is not None and value > 10000 else value

        first, second = seconds(source), seconds(candidate)
        same_album = bool(album(source) and album(source) == album(candidate))

        # A song-named single may omit its guest credit in Plex's release name.
        # Keep this separate from album normalization: a featured single must
        # never become equivalent to an unrelated studio album.
        def single_name(track):
            value = re.sub(r"\s*-\s*(?:single|ep)$", "", str(track.get("album") or ""), flags=re.I)
            value = re.sub(
                r"\s*[\(\[]\s*(?:feat(?:uring)?\.?|ft\.?)\s+[^)\]]+[\)\]]", "", value, flags=re.I
            )
            return cls._normalize_match_text(value)

        core_title = cls._normalize_match_text(
            cls._strip_title_metadata(str(source.get("title") or ""))
        )
        same_single = bool(
            core_title and single_name(source) == core_title == single_name(candidate)
        )
        if (
            (same_album or same_single)
            and lead(source)
            and lead(source) == lead(candidate)
            and cls._normalize_match_text(cls._strip_title_metadata(str(source.get("title") or "")))
            == cls._normalize_match_text(
                cls._strip_title_metadata(str(candidate.get("title") or ""))
            )
            and first is not None
            and second is not None
            and abs(first - second) <= 2
        ):
            return "Guest credit omitted; album and duration agree"
        return "Conflict: featured recording needs matching credits or recording evidence"

    @classmethod
    @lru_cache(maxsize=65536)
    def _title_parts(cls, title):
        raw = re.sub(
            r"\s*(?:[([]\s*taylors?\s+version\s*[)\]]|[-:]\s*taylors?\s+version)\s*",
            " ",
            cls._normalize_match_text(title),
        ).strip()
        abbrev = re.sub(r"(?<=[a-z])\.(?=(?:[a-z]\.)|(?:\s|$))", "", raw, flags=re.I)
        meta = cls._strip_title_metadata(raw)
        base, trailing = cls._strip_trailing_parenthetical(meta)
        return raw, abbrev, meta, base, trailing

    @classmethod
    @lru_cache(maxsize=4)
    def _title_choices(cls, titles):
        # Only immutable title strings are shared. No account records or decisions.
        return tuple(
            fuzz.sorted_tokens(part) for title in titles for part in cls._title_parts(title)[:3]
        )

    @classmethod
    def _strong_title_indices(cls, source_title, library):
        from rapidfuzz import process, fuzz as rapid_fuzz

        titles = tuple(str(row.get("title", "")) for row in library)
        flat = cls._title_choices(titles)
        # A conservative superset of every comparison in _title_score. The
        # original scorer verifies one-sided subtitles and integer rounding.
        forms = set(fuzz.sorted_tokens(part) for part in cls._title_parts(source_title)[:4])
        indices = set()
        for form in forms:
            indices.update(
                index // 3
                for _, _, index in process.extract_iter(
                    form, flat, scorer=rapid_fuzz.ratio, processor=None, score_cutoff=94.5
                )
            )
        return sorted(
            index for index in indices if cls._title_score(source_title, titles[index])[0] >= 95
        )

    @classmethod
    @lru_cache(maxsize=65536)
    def _title_score(cls, source_title: str, plex_title: str) -> Tuple[int, int]:
        """
        Return (best_title_score, raw_title_score).

        The best score considers:
        - literal titles;
        - safe metadata-stripped titles;
        - a one-sided arbitrary trailing-parenthetical fallback.

        The one-sided rule is what allows:
            Austin (Boots Stop Workin') <-> Austin
        without making:
            Song (Part 1) <-> Song (Part 2)
        an artificial 100% match.
        """

        source_raw, source_abbrev, source_meta, source_base, source_had_trailing = cls._title_parts(
            source_title
        )
        plex_raw, plex_abbrev, plex_meta, _, plex_had_trailing = cls._title_parts(plex_title)

        raw_score = fuzz.token_sort_ratio(
            source_raw,
            plex_raw,
        )

        scores = [raw_score]

        if source_abbrev and plex_abbrev:
            scores.append(
                fuzz.token_sort_ratio(
                    source_abbrev,
                    plex_abbrev,
                )
            )

        if source_meta and plex_meta:
            scores.append(
                fuzz.token_sort_ratio(
                    source_meta,
                    plex_meta,
                )
            )

        if source_meta and plex_raw:
            scores.append(
                fuzz.token_sort_ratio(
                    source_meta,
                    plex_raw,
                )
            )

        if source_raw and plex_meta:
            scores.append(
                fuzz.token_sort_ratio(
                    source_raw,
                    plex_meta,
                )
            )

        # Generic parenthetical fallback only if ONE side has it.
        if source_had_trailing and not plex_had_trailing:
            if source_base and plex_meta:
                scores.append(
                    fuzz.token_sort_ratio(
                        source_base,
                        plex_meta,
                    )
                )

        # Deliberately do NOT apply the arbitrary fallback in reverse.
        # A destination-only qualifier can identify a different recording or
        # arrangement, e.g.:
        #   Bring Me to Life -> Bring Me to Life (Synthesis)
        #
        # Known metadata such as feat/remaster/live/remix is already handled
        # by _strip_title_metadata() and the release-type penalties below.

        return max(scores), raw_score

    @classmethod
    @lru_cache(maxsize=65536)
    def _title_remix_credit(cls, title: str) -> str:
        """
        Extract a named remix credit from a title.

        Example:
            Dracula (JENNIE Remix) -> jennie

        A generic "(Remix)" does not identify a collaborator and therefore
        returns an empty string.
        """
        value = cls._normalize_match_text(title)

        if not value:
            return ""

        match = re.search(
            r"[\(\[]\s*(.+?)\s+remix\s*[\)\]]",
            value,
            flags=re.IGNORECASE,
        )

        if not match:
            return ""

        credit = match.group(1).strip()

        if not credit or credit == "remix":
            return ""

        return credit

    @classmethod
    @lru_cache(maxsize=65536)
    def _title_has_feature_credit(cls, title: str) -> bool:
        """Return True when a title explicitly identifies a featured guest."""
        value = cls._normalize_match_text(title)

        if not value:
            return False

        return bool(
            re.search(
                r"(?:^|[\s(\[\-:])"
                r"(?:feat(?:uring)?\.?|ft\.?|with)"
                r"\s+",
                value,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def set_artist_aliases(
        cls,
        alias_groups: dict,
    ):
        """Configure symmetric global artist alias equivalence groups."""
        graph = {}

        if not isinstance(alias_groups, dict):
            cls._alias_context.set({})
            return

        for canonical, aliases in alias_groups.items():
            canonical_norm = cls._normalize_match_text(canonical)

            if not canonical_norm:
                continue

            if isinstance(aliases, str):
                alias_values = [aliases]
            elif isinstance(aliases, list):
                alias_values = aliases
            else:
                continue

            members = {canonical_norm}

            for alias in alias_values:
                alias_norm = cls._normalize_match_text(alias)
                if alias_norm:
                    members.add(alias_norm)

            for member in members:
                graph.setdefault(member, set()).update(members - {member})

        lookup = {}
        visited = set()

        for start in graph:
            if start in visited:
                continue

            stack = [start]
            component = set()

            while stack:
                current = stack.pop()
                if current in component:
                    continue

                component.add(current)
                stack.extend(graph.get(current, set()) - component)

            visited.update(component)
            ordered = sorted(component)

            for member in component:
                lookup[member] = ordered

        cls._alias_context.set(lookup)

    @classmethod
    def _expand_artist_aliases(
        cls,
        variants: List[str],
    ) -> List[str]:
        """Expand normalized artist variants through global aliases."""
        expanded = []

        for variant in variants:
            if variant and variant not in expanded:
                expanded.append(variant)

            for alias in (
                cls._alias_context.get()
                if cls._alias_context.get() is not None
                else cls.ARTIST_ALIAS_LOOKUP
            ).get(
                variant,
                [],
            ):
                if alias and alias not in expanded:
                    expanded.append(alias)

        return expanded

    @classmethod
    @lru_cache(maxsize=65536)
    def _artist_variants(
        cls,
        artist: str,
        allow_primary_collaborator: bool = False,
    ) -> List[str]:
        """
        Generate conservative artist variants.

        When the source title explicitly says "feat." (or equivalent), the
        first/primary portion of a collaboration string is also considered.
        This allows:
            AWOLNATION & Nothing But Thieves -> AWOLNATION
        for:
            Maniac (feat. Conor Mason of Nothing but Thieves)

        We only enable that behavior when the track title itself tells us the
        additional artist is a feature, so ordinary co-billed artists are not
        silently discarded.
        """
        value = cls._normalize_match_text(artist)

        if not value:
            return ("",)

        variants = [value]
        if value.startswith("the ") and len(value) > 4:
            variants.append(value[4:])

        # Artist-string feature syntax.
        primary = re.sub(
            r"\s+(?:feat(?:uring)?\.?|ft\.?|with)\s+.+$",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip()

        if primary and primary not in variants:
            variants.append(primary)

        if allow_primary_collaborator:
            # Add progressively shorter collaboration prefixes. This is safer
            # than blindly taking only the first token because artist names
            # can themselves contain "&".
            for separator in (" & ", " x ", " and "):
                if separator not in value:
                    continue

                parts = value.split(separator)

                # All prefixes except the complete original value.
                for end in range(1, len(parts)):
                    candidate = separator.join(parts[:end]).strip()
                    if candidate and candidate not in variants:
                        variants.append(candidate)

        return tuple(variants)

    @classmethod
    def _artist_score(
        cls,
        source_artist: str,
        plex_artist: str,
        source_title: str = "",
        plex_title: str = "",
    ) -> int:
        """Return the best conservative artist score."""
        remix_credit = cls._title_remix_credit(source_title)

        allow_source_primary = cls._title_has_feature_credit(source_title) or (
            bool(remix_credit) and remix_credit in cls._normalize_match_text(source_artist)
        )

        source_variants = cls._expand_artist_aliases(
            cls._artist_variants(
                source_artist,
                allow_primary_collaborator=allow_source_primary,
            )
        )

        plex_variants = cls._expand_artist_aliases(
            cls._artist_variants(
                plex_artist,
                allow_primary_collaborator=cls._title_has_feature_credit(plex_title),
            )
        )

        return max(
            fuzz.ratio(source_variant, plex_variant)
            for source_variant in source_variants
            for plex_variant in plex_variants
        )

    @classmethod
    @lru_cache(maxsize=65536)
    def _title_release_types(cls, title: str) -> set:
        """
        Detect explicit recording/version intent in a track title.

        Featured-artist credits are deliberately excluded because they may
        appear on only one service while still referring to the same track.
        """
        value = cls._normalize_match_text(title)

        if not value:
            return set()

        kinds = set()

        # Remix and named Mix variants. "Radio Mix" / "Radio Edit" are
        # intentionally treated as equivalent radio-version metadata, not as
        # remix intent.
        remix_value = re.sub(
            r"\bradio\s+(?:mix|edit)\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )

        remix_patterns = (
            r"\bremix(?:ed|es)?\b",
            r"\bmixes\b",
            r"[\(\[][^)\]]+\s+mix\b[^)\]]*[\)\]]",
            r"\s[-:]\s*[^-:]*\bmix\b.*$",
        )

        if any(re.search(pattern, remix_value, flags=re.IGNORECASE) for pattern in remix_patterns):
            kinds.add("remix")

        # Live is deliberately conservative so a real title such as
        # "Live Through This" is not treated as a live recording.
        live_patterns = (
            r"[\(\[]\s*live\b[^)\]]*[\)\]]",
            r"\s[-:]\s*live(?:\s+(?:at|from|in|on)\b.*)?$",
            r"\blive\s+(?:at|from|in|on)\b",
            r"\bunplugged\b",
            r"\bin concert\b",
        )

        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in live_patterns):
            kinds.add("live")

        acoustic_patterns = (
            r"[\(\[][^)\]]*\bacoustic\b[^)\]]*[\)\]]",
            r"\s[-:]\s*[^-:]*\bacoustic\b.*$",
            r"[\(\[][^)\]]*\bstripped\b[^)\]]*[\)\]]",
            r"\s[-:]\s*[^-:]*\bstripped\b.*$",
        )

        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in acoustic_patterns):
            kinds.add("acoustic")

        demo_patterns = (
            r"[\(\[][^)\]]*\bdemo\b[^)\]]*[\)\]]",
            r"\s[-:]\s*[^-:]*\bdemo\b.*$",
        )

        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in demo_patterns):
            kinds.add("demo")

        # Distinct session recordings include branded platform sessions and
        # clear broadcast/studio-performance session labels. Generic album-era
        # provenance such as "(Evolver Sessions)" is deliberately not enough
        # on its own to create session intent.
        session_marker = (
            r"(?:itunes|apple(?:\s+music)?|spotify|bbc|kexp|npr|"
            r"peel|maida\s+vale|electro[-\s]?vox)"
        )
        session_patterns = (
            rf"[\(\[][^)\]]*\b{session_marker}\b[^)\]]*"
            r"\bsessions?\b[^)\]]*[\)\]]",
            rf"\s[-:]\s*[^-:]*\b{session_marker}\b.*"
            r"\bsessions?\b.*$",
            r"[\(\[][^)\]]*\b(?:live|acoustic)\s+sessions?\b"
            r"[^)\]]*[\)\]]",
        )

        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in session_patterns):
            kinds.add("session")

        return kinds

    @staticmethod
    @lru_cache(maxsize=65536)
    def _normalize_album(album: str) -> str:
        """Normalize an album title for fuzzy album comparison."""
        if not album:
            return ""

        value = str(album).casefold()
        value = value.replace("’", "'").replace("–", "-").replace("—", "-")

        # Remove common edition qualifiers while keeping the core album name.
        value = re.sub(
            r"[\(\[][^)\]]*"
            r"(?:remaster(?:ed)?|deluxe|expanded|anniversary|"
            r"bonus|special edition|collector'?s edition)"
            r"[^)\]]*[\)\]]",
            " ",
            value,
            flags=re.IGNORECASE,
        )

        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    @lru_cache(maxsize=65536)
    def _album_identity(cls, album: str) -> str:
        """Compare specific album names, retaining volume and recording credits."""
        value = re.sub(r"\s*-\s*(?:single|ep)$", "", str(album or ""), flags=re.I)
        # Only a delimited descriptive prefix on an otherwise named album.
        # Never collapse standalone Greatest Hits or distinct featured singles.
        value = re.sub(r"^\s*greatest hits\s*:\s*(?=\S)", "", value, flags=re.I)
        return cls._normalize_album(value)

    @classmethod
    @lru_cache(maxsize=65536)
    def _source_requires_album_provenance(
        cls,
        album: str,
    ) -> bool:
        """
        Return True when the source album name carries recording provenance
        that should not be silently discarded during automatic matching.

        This is deliberately narrow. Normal studio/edition/compilation album
        names keep the existing canonical-copy behavior. Archive/rarity
        collections can contain alternate or otherwise specific recordings,
        so an unrelated album copy should be reviewed instead of substituted.
        """
        value = cls._normalize_album(album)

        if not value:
            return False

        patterns = (
            r"\barchive\b",
            r"\brarities?\b",
            r"\bb sides?\b",
            r"\bouttakes?\b",
        )

        return any(
            re.search(
                pattern,
                value,
                flags=re.IGNORECASE,
            )
            for pattern in patterns
        )

    @staticmethod
    @lru_cache(maxsize=65536)
    def _album_types(album: str) -> set:
        """
        Classify release types we normally want to rank below a studio album.

        The patterns are intentionally conservative so an album whose actual
        title happens to contain a word like "live" is less likely to be
        penalized accidentally.
        """
        if not album:
            return set()

        value = str(album).casefold()
        kinds = set()

        compilation_patterns = (
            r"\bgreatest hits\b",
            r"\bbest of\b",
            r"\bthe essential\b",
            r"\bessential[s]?\b",
            r"\banthology\b",
            r"\bsingles collection\b",
            r"\bcomplete collection\b",
            r"\bhits collection\b",
            r"\bcollected\b",
        )

        live_patterns = (
            r"^live$",
            r"\blive at\b",
            r"\blive from\b",
            r"\blive in\b",
            r"\blive on\b",
            r"\blive album\b",
            r"\bunplugged\b",
            r"\bin concert\b",
            r"\bconcert recording\b",
        )

        remix_patterns = (
            r"\bremix(?:es)?\b",
            r"\bremixed\b",
            r"\bmixes\b",
        )

        acoustic_patterns = (
            r"\bacoustic\b",
            r"\bstripped\b",
        )

        demo_patterns = (r"\bdemo(?:s)?\b",)

        session_patterns = (
            r"\b(?:itunes|apple(?:\s+music)?|spotify)\s+"
            r"(?:home\s+)?sessions?\b",
        )

        if any(re.search(p, value) for p in compilation_patterns):
            kinds.add("compilation")
        if any(re.search(p, value) for p in live_patterns):
            kinds.add("live")
        if any(re.search(p, value) for p in remix_patterns):
            kinds.add("remix")
        if any(re.search(p, value) for p in acoustic_patterns):
            kinds.add("acoustic")
        if any(re.search(p, value) for p in demo_patterns):
            kinds.add("demo")
        if any(re.search(p, value) for p in session_patterns):
            kinds.add("session")

        return kinds

    @classmethod
    def score_candidate(
        cls,
        source_track: dict,
        plex_track: dict,
    ) -> dict:
        """
        Score one Plex candidate.

        Title/artist determine identity. Album information only changes the
        ranking among plausible copies of the same song.

        This is deliberate: a Greatest Hits copy can still be used when it is
        the only copy in Plex, but a studio-album copy should win when both
        exist.
        """
        source_title = str(source_track.get("title", ""))
        source_artist = str(source_track.get("artist", ""))
        source_album = str(source_track.get("album", "") or "")

        plex_title = str(plex_track.get("title", ""))
        plex_artist = str(plex_track.get("artist", ""))
        plex_album = str(plex_track.get("album", "") or "")

        feature_reason = cls._feature_identity_reason(source_track, plex_track)
        version_mismatch = cls._taylors_version(source_track) != cls._taylors_version(
            plex_track
        ) or feature_reason.startswith("Conflict:")

        title_score, raw_title_score = cls._title_score(
            source_title,
            plex_title,
        )

        artist_score = cls._artist_score(
            source_artist,
            plex_artist,
            source_title=source_title,
            plex_title=plex_title,
        )

        # A source may carry structured credits while Plex stores only the lead.
        # For unstructured text, accept a comma-separated lead only when the
        # remaining credit explicitly contains a collaboration separator. A
        # lone ampersand in a band name is never sufficient evidence.
        credit_reason = ""
        from .artist_credits import structured

        credits = structured(source_track)
        lead = ""
        if credits and isinstance(credits, list):
            lead = credits[0].get("name", "") if isinstance(credits[0], dict) else str(credits[0])
        elif (
            source_album.lower().endswith(" - single")
            and cls._normalize_album(source_album[:-9]) == cls._normalize_album(source_title)
            and "," in source_artist
            and re.search(r"\s(?:&|and)\s", source_artist.split(",", 1)[1], re.I)
        ):
            lead = source_artist.split(",", 1)[0].strip()
        credit_names = [c.get("name", "") if isinstance(c, dict) else str(c) for c in credits]
        matched_credit = next(
            (
                name
                for name in credit_names
                if cls._normalize_match_text(name) == cls._normalize_match_text(plex_artist)
            ),
            lead
            if lead and cls._normalize_match_text(lead) == cls._normalize_match_text(plex_artist)
            else "",
        )
        if matched_credit and title_score >= 95:
            left = re.sub(r"\s*-\s*(single|ep)$", "", source_album, flags=re.I)
            right = re.sub(r"\s*-\s*(single|ep)$", "", plex_album, flags=re.I)
            if left and cls._normalize_album(left) == cls._normalize_album(right):
                artist_score = max(artist_score, 95)
                credit_reason = "A structured contributor matches; title and album agree"

        # Duration supports a verified lead credit when provider/Plex guest
        # lists differ. It never substitutes for artist or song identity.
        if not lead and cls._title_has_feature_credit(source_title) and "," in source_artist:
            lead = source_artist.split(",", 1)[0].strip()
        if lead and title_score >= 95 and not version_mismatch:
            lead_score = cls._artist_score(lead, plex_artist, source_title, plex_title)
            from .track_duration import source_duration

            left = source_duration(source_track)
            right = source_duration(plex_track)
            if left is not None and right is not None:
                left = left / 1000 if left > 10000 else left
                right = right / 1000 if right > 10000 else right
                if lead_score >= 95 and abs(left - right) <= 5:
                    artist_score = max(artist_score, 95)
                    credit_reason = "Lead artist and song title agree; duration is within 5 seconds"

        # Preserve the existing strong artist penalty.
        weighted_artist = artist_score
        if artist_score < 70:
            weighted_artist *= 0.3

        identity_score = title_score * 0.65 + weighted_artist * 0.35

        album_score = None
        album_bonus = 0.0

        if source_album and plex_album:
            source_album_norm = cls._normalize_album(source_album)
            plex_album_norm = cls._normalize_album(plex_album)

            if source_album_norm and plex_album_norm:
                album_score = fuzz.token_set_ratio(
                    source_album_norm,
                    plex_album_norm,
                )

                # Reward the intended album only after title identity is
                # already plausible. This prevents a wrong song by the same
                # artist on the same album from outranking the correct title.
                if title_score >= 95:
                    if album_score >= 95:
                        album_bonus = 12
                    elif album_score >= 85:
                        album_bonus = 8
                    elif album_score >= 70:
                        album_bonus = 4

                elif title_score >= 85:
                    # A moderate title match may receive only a small nudge.
                    if album_score >= 95:
                        album_bonus = 4
                    elif album_score >= 85:
                        album_bonus = 2

        source_album_requires_provenance = cls._source_requires_album_provenance(source_album)
        album_provenance_mismatch = False
        album_provenance_penalty = 0

        if source_album_requires_provenance:
            album_provenance_mismatch = album_score is None or album_score < 85

            if album_provenance_mismatch:
                album_provenance_penalty = 12
                album_bonus = 0.0

        source_types = cls._album_types(source_album)
        plex_types = cls._album_types(plex_album)

        source_title_types = cls._title_release_types(source_title)
        plex_title_types = cls._title_release_types(plex_title)

        # Recording/version markers in the SOURCE TITLE are explicit intent.
        #
        # Example:
        #   Dracula (JENNIE Remix)
        #
        # In that case a Plex remix copy is correct and should not receive
        # the normal remix penalty. The same applies to an explicitly live
        # source title.
        requested_variant_types = source_title_types & cls.VERSION_TYPES

        candidate_variant_types = (plex_title_types | plex_types) & cls.VERSION_TYPES

        # Canonical-copy preference still applies to release types the source
        # did NOT explicitly request in its title.
        unwanted_plex_types = plex_types - requested_variant_types

        if cls._album_identity(source_album) and cls._album_identity(
            source_album
        ) == cls._album_identity(plex_album):
            # Different labels for the same compilation are not a substitution.
            unwanted_plex_types = unwanted_plex_types - {"compilation"}

        album_penalty = sum(cls.ALBUM_TYPE_PENALTIES[kind] for kind in unwanted_plex_types)

        # A destination title can reveal an unwanted version even when the
        # album does not. Avoid double-penalizing a type already caught by
        # the album classification.
        unwanted_title_variant_types = (
            (plex_title_types & cls.VERSION_TYPES) - requested_variant_types - unwanted_plex_types
        )

        title_variant_penalty = sum(
            cls.ALBUM_TYPE_PENALTIES[kind] for kind in unwanted_title_variant_types
        )

        # If the source explicitly asks for a recording variant, title-level evidence
        # is stronger than album-level evidence:
        #
        #   candidate title says Remix/Live -> no intent penalty
        #   only candidate album says it    -> half penalty
        #   neither says it                 -> full penalty
        #
        # This keeps "Dracula (JENNIE remix)" above plain "Dracula" even when
        # both Plex tracks happen to live on the same remix album.
        release_intent_penalty = 0

        for kind in requested_variant_types:
            full_penalty = cls.ALBUM_TYPE_PENALTIES[kind]

            if kind in plex_title_types:
                continue

            if kind in plex_types:
                release_intent_penalty += max(
                    1,
                    full_penalty // 2,
                )
            else:
                release_intent_penalty += full_penalty

        # Do not allow an album bonus to erase a missing title-level recording variant
        # marker. A plain track on a remix/live album remains viable but ranks
        # below a candidate whose title explicitly matches the source intent.
        missing_title_intent = requested_variant_types - plex_title_types

        if unwanted_plex_types or unwanted_title_variant_types or missing_title_intent:
            album_bonus = 0.0

        if feature_reason:
            credit_reason = feature_reason
        if version_mismatch:
            album_provenance_mismatch = True
            album_provenance_penalty = max(album_provenance_penalty, 40)
            album_bonus = 0.0

        def seconds(value):
            try:
                value = float(value)
                return value / 1000 if value > 10000 else value
            except (TypeError, ValueError):
                return None

        source_seconds, plex_seconds = (
            seconds(source_track.get("duration")),
            seconds(plex_track.get("duration")),
        )
        duration_delta = (
            abs(source_seconds - plex_seconds)
            if source_seconds is not None and plex_seconds is not None
            else None
        )
        duration_penalty = (
            0
            if duration_delta is None or duration_delta <= 2
            else min(12, (duration_delta - 2) * 1.5)
        )
        adjusted_score = max(
            0.0,
            min(
                100.0,
                identity_score
                + album_bonus
                - album_penalty
                - title_variant_penalty
                - release_intent_penalty
                - album_provenance_penalty,
            )
            - duration_penalty,
        )

        return {
            "adjusted_score": adjusted_score,
            "identity_score": identity_score,
            "artist_credit_reason": credit_reason,
            "source_duration": source_seconds,
            "candidate_duration": plex_seconds,
            "duration_delta": duration_delta,
            "duration_penalty": duration_penalty,
            "title_score": title_score,
            "raw_title_score": raw_title_score,
            "artist_score": artist_score,
            "album_score": album_score,
            "album_bonus": album_bonus,
            "album_penalty": album_penalty,
            "title_variant_penalty": title_variant_penalty,
            "release_intent_penalty": release_intent_penalty,
            "album_provenance_penalty": album_provenance_penalty,
            "album_provenance_mismatch": album_provenance_mismatch,
            "source_album_requires_provenance": (source_album_requires_provenance),
            "source_album_types": source_types,
            "plex_album_types": plex_types,
            "source_title_release_types": source_title_types,
            "plex_title_release_types": plex_title_types,
            "requested_variant_types": requested_variant_types,
            "candidate_variant_types": candidate_variant_types,
            "candidate_title_variant_types": (plex_title_types & cls.VERSION_TYPES),
            "candidate_album_variant_types": (plex_types & cls.VERSION_TYPES),
        }

    @classmethod
    def match_track(cls, source_track, plex_library, mapping_cache=None):
        from .track_bridge import match

        return match(cls, source_track, plex_library, mapping_cache)

    @classmethod
    def _match_catalog_track(
        cls,
        source_track: dict,
        plex_library: List[dict],
        mapping_cache: dict = None,
    ) -> Optional[str]:
        """Match source track to Plex while preferring canonical album copies."""

        if mapping_cache is None:
            mapping_cache = {}

        search_key = f"{source_track['title']}|{source_track['artist']}"

        if search_key in mapping_cache:
            cached = next(
                (
                    track
                    for track in plex_library
                    if str(track.get("plex_id")) == str(mapping_cache[search_key])
                ),
                None,
            )
            if (
                cached is not None
                and not cls._recording_version_mismatch(source_track, cached)
                and cls._feature_identity_reason(source_track, cached)
                != "Guest credit omitted; album and duration agree"
            ):
                return mapping_cache[search_key]

        if not plex_library:
            return None

        # The existing decision always isolates title>=95 / artist>=85
        # identities before ranking. Find that same complete pool first and
        # avoid expensive album/credit scoring for unrelated titles.
        strong = []
        pre_scored = {}
        for index in cls._strong_title_indices(str(source_track.get("title", "")), plex_library):
            plex_track = plex_library[index]
            if cls._recording_version_mismatch(source_track, plex_track):
                continue
            details = cls.score_candidate(source_track, plex_track)
            pre_scored[index] = details
            if details["title_score"] >= 95 and details["artist_score"] >= 85:
                strong.append((details, plex_track))
        if strong:
            scored = strong
        else:
            # Preserve the full fuzzy fallback and its original tie order.
            scored = []
            for index, plex_track in enumerate(plex_library):
                details = pre_scored.get(index)
                if details is None:
                    if cls._recording_version_mismatch(source_track, plex_track):
                        continue
                    details = cls.score_candidate(source_track, plex_track)
                scored.append((details, plex_track))

        if not scored:
            return None

        # First isolate strong title+artist identities. This prevents a
        # mediocre studio-album candidate from beating an exact song match
        # merely because the exact copy is on a compilation.
        strong_identity = [
            item
            for item in scored
            if (item[0]["title_score"] >= 95 and item[0]["artist_score"] >= 85)
        ]

        candidate_pool = strong_identity or scored

        best_details, best_track = max(
            candidate_pool,
            key=lambda item: (
                item[0]["adjusted_score"],
                item[0]["identity_score"],
                item[0]["title_score"],
                item[0]["raw_title_score"],
                item[0]["album_score"] or 0,
            ),
        )

        if (
            best_details.get("artist_credit_reason")
            == "Guest credit omitted; album and duration agree"
        ):
            for details, alternate in candidate_pool:
                if str(alternate.get("plex_id")) == str(best_track.get("plex_id")):
                    continue
                if (
                    details["title_score"] >= 95
                    and details["artist_score"] >= 85
                    and abs(details["adjusted_score"] - best_details["adjusted_score"]) <= 2
                    and not (
                        alternate.get("recording_id")
                        and alternate.get("recording_id") == best_track.get("recording_id")
                    )
                ):
                    return None

        # Automatic matching has two confidence paths:
        #
        # 1) Strong title/artist identity (original behavior).
        # 2) A near-exact normalized title with a strong final score.
        #
        # The second path keeps initial sync consistent with the score shown
        # in Option 5 when source-service artist credits differ, while the
        # strict title/artist gates prevent album metadata from rescuing a
        # clearly different song.
        missing_requested_variant_types = (
            best_details["requested_variant_types"] - best_details["candidate_variant_types"]
        )

        # A different recording requires review even when song identity is exact.
        # Album metadata can explicitly request live as well as the source title.
        source_live = "live" in (
            cls._title_release_types(source_track.get("title", ""))
            | cls._album_types(source_track.get("album", ""))
        )
        if "live" in best_details["candidate_variant_types"] and not source_live:
            return None

        strong_identity_match = (
            best_details["identity_score"] >= cls.MATCH_THRESHOLD
            and best_details["title_score"] >= 75
            and not missing_requested_variant_types
            and not best_details["album_provenance_mismatch"]
        )

        strong_adjusted_match = (
            best_details["adjusted_score"] >= cls.MATCH_THRESHOLD
            and best_details["title_score"] >= 95
            and best_details["artist_score"] >= 70
            and not missing_requested_variant_types
            and not best_details["album_provenance_mismatch"]
        )

        if strong_identity_match or strong_adjusted_match:
            return best_track["plex_id"]

        return None
