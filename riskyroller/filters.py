"""Free-text guard for player-typed questions and replies.

Both are posted publicly by the bot under its own name, so they share one
slur/abuse check before they go out. The text is normalised first — Unicode
format and mark characters and non-ASCII spacing are stripped, common
Cyrillic/Greek look-alike letters are folded to Latin, and the result is
NFKC-folded — which makes the denylist harder to bypass with invisible
padding or lookalike Unicode (fullwidth letters, homoglyphs). This is
best-effort screening, not a guarantee: it is a literal-pattern denylist and
cannot catch every way a slur can be spelled or disguised.
"""

import re
import unicodedata

# Cyrillic and Greek letters that render identically (or near enough) to the
# Latin vowels the denylist below actually uses. Deliberately small and
# non-exhaustive — just the common look-alikes for a, e, i and o.
_CONFUSABLES = {
    "а": "a", "е": "e", "і": "i", "о": "o",
    "А": "A", "Е": "E", "І": "I", "О": "O",
    "α": "a", "ε": "e", "ι": "i", "ο": "o",
    "Α": "A", "Ε": "E", "Ι": "I", "Ο": "O",
}

# Unicode general categories stripped before matching: Cf (format, e.g.
# zero-width space/joiner, soft hyphen), Mn (nonspacing marks, e.g. a
# combining underline) and Zs (space separators, e.g. hair space) — all
# commonly used to pad a slur apart so a literal pattern misses it. A plain
# ASCII space is kept so word-separated matches are unaffected.
_STRIP_CATEGORIES = {"Cf", "Mn", "Zs"}

# Trailing boundary that still stops a match spilling into a longer,
# unrelated word (e.g. "retarding") but tolerates the padding a bypass
# attempt tacks on: a plural/leet "s"/"z" or trailing digits.
_SUFFIX = r"(?:[sz]|[0-9])*(?![a-zA-Z])"

# Regexes matched case-insensitively against the normalised text. Leading
# \b keeps a short entry from matching inside a longer word it merely sits
# inside; see _SUFFIX above for the trailing side.
DENYLIST: list[str] = [
    r"\bn[i1]gg[ae3]r" + _SUFFIX,
    r"\bf[a@]gg[o0]t" + _SUFFIX,
    r"\br[e3]t[a@]rd" + _SUFFIX,
]


def _clean(text: str) -> str:
    # Strip padding characters before NFKC folding: several of the space
    # variants (e.g. hair space) normalise to a plain ASCII space, which
    # would otherwise re-introduce the separator this step removes.
    stripped = "".join(
        ch for ch in text if ch == " " or unicodedata.category(ch) not in _STRIP_CATEGORIES
    )
    folded = unicodedata.normalize("NFKC", stripped)
    return "".join(_CONFUSABLES.get(ch, ch) for ch in folded).strip()


def contains_disallowed_content(text: str) -> bool:
    """Best-effort check: True if the cleaned text matches the denylist."""
    cleaned = _clean(text)
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in DENYLIST)
