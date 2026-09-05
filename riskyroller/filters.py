"""Free-text guard for player-typed questions and replies.

Both are posted publicly by the bot under its own name, so they share one
slur/abuse check before they go out. The text is normalised first — zero-width
characters stripped and NFKC-folded — so the denylist cannot be bypassed with
invisible padding or lookalike Unicode (fullwidth letters, homoglyphs).
"""

import re
import unicodedata

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# Regexes matched case-insensitively against the normalised text. Whole-word
# boundaries keep a short entry from swallowing longer words it merely sits
# inside.
DENYLIST: list[str] = [
    r"\bn[i1]gg[ae3]r\b",
    r"\bf[a@]gg[o0]t\b",
    r"\br[e3]t[a@]rd\b",
]


def _clean(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text)
    return unicodedata.normalize("NFKC", text).strip()


def contains_disallowed_content(text: str) -> bool:
    """True if the cleaned text matches the denylist."""
    cleaned = _clean(text)
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in DENYLIST)
