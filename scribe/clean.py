"""Filler removal and paragraph rendering.

Two versions of every rant get written: the raw one and this one. That pairing
is what makes the cleaning safe to be aggressive about — nothing here has to be
conservative, because the verbatim text is always sitting next to it.

The tiering exists because filler words are not one category. "Um" carries no
meaning and can always go. "I mean" is usually filler but sometimes a genuine
self-correction. "Actually" often changes the sentence. So they're separated by
how much is lost when the guess is wrong, and only the safe tier runs by default.
"""

from __future__ import annotations

import re

# Immediate word repetition: "the the thing" -> "the thing". Catches both the
# stutter and the restart-after-a-pause, which is the same artefact.
_STUTTER = re.compile(r"\b(\w+)(\s+\1\b)+", re.IGNORECASE)

_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


def _alternation(words: list[str]) -> str:
    # Longest first so "you know" wins over a bare "know" prefix match.
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def _strip_everywhere(text: str, words: list[str]) -> str:
    """Delete each word wherever it appears, absorbing the punctuation it strands.

    Only safe for sounds with no meaning of their own — "um", "uh". Applying it
    to real words is how "Do you know the answer?" became "Do the answer?".
    """
    if not words:
        return text
    alt = _alternation(words)
    # An interstitial filler sits between two commas — "I was, uh, thinking".
    # Both commas have to go with it, or the clause is left limping: "I was,
    # thinking". Handled first, because the general rule below only takes the
    # trailing one.
    text = re.sub(rf",\s*\b(?:{alt})\b\s*,", " ", text, flags=re.IGNORECASE)
    text = re.sub(rf"\b(?:{alt})\b\s*,?\s*", " ", text, flags=re.IGNORECASE)
    return text


def _strip_delimited(text: str, words: list[str]) -> str:
    """Delete a phrase only where it is set off by punctuation as an aside.

    "You know", "I mean", "kind of" and "sort of" are filler when they are
    parenthetical and grammar when they aren't:

        filler   So, you know, I tried it.      grammar  Do you know the answer?
        filler   I mean, that was the plan.     grammar  What I mean is the API changed.
        filler   It was bad, you know.          grammar  The build is kind of broken.

    The dividing line is punctuation. A phrase is removed only when it has a
    comma on at least one side, and the other side is a comma, a sentence
    boundary, or the end of the text. With no comma at all it is part of the
    sentence and is left alone — which also keeps a one-word answer like
    "Kind of." intact.
    """
    if not words:
        return text
    pattern = re.compile(
        rf"(?P<lead>,\s*|(?:^|(?<=[.!?]))\s*)"
        rf"(?P<word>\b(?:{_alternation(words)})\b)"
        rf"(?P<trail>\s*,|\s*(?=[.!?])|\s*$)",
        re.IGNORECASE,
    )

    def replace(m: re.Match) -> str:
        lead_comma = m.group("lead").lstrip().startswith(",")
        trail_comma = m.group("trail").lstrip().startswith(",")
        if not (lead_comma or trail_comma):
            return m.group(0)          # part of the sentence: keep it
        if lead_comma and trail_comma:
            return " "                 # ", you know," -> both commas go
        if trail_comma:
            return m.group("lead")     # "I mean, that" -> keep what preceded
        return ""                      # ", you know." -> leading comma goes too

    return pattern.sub(replace, text)


def _tidy(text: str) -> str:
    """Repair the punctuation that removal leaves stranded."""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)   # " ," -> ","
    text = re.sub(r"([,;:])\s*(?=[,.;:!?])", "", text)  # ", ." -> "."
    # A filler that was a whole sentence ("Hmm.") leaves its full stop behind.
    text = re.sub(r"^[\s,;:.!?]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Removing a leading "Um," lowercases the sentence; put the capital back.
    text = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def clean_text(text: str, cfg: dict) -> str:
    rules = cfg["cleaning"]
    text = _strip_everywhere(text, rules.get("always") or [])
    text = _strip_delimited(text, rules.get("phrases") or [])
    text = _strip_delimited(text, rules.get("aggressive") or [])
    if rules.get("fix_stutters", True):
        text = _STUTTER.sub(r"\1", text)
    return _tidy(text)


# --- paragraph assembly ------------------------------------------------------

def to_paragraphs(segments: list, breaks: set[int]) -> list[str]:
    """Join segments into paragraphs, breaking where a thinking pause was.

    ``breaks`` holds the indices of segments that open a new paragraph. The
    pause information comes from the chunk planner, which already had to find
    the silences in order to skip them — so the structure is free, and it maps
    to where you actually stopped to work something out.
    """
    if not segments:
        return []

    paras: list[str] = []
    current: list[str] = []
    for i, seg in enumerate(segments):
        if i in breaks and current:
            paras.append(" ".join(current))
            current = []
        text = seg.text.strip()
        if text:
            current.append(text)
    if current:
        paras.append(" ".join(current))
    return [p for p in (_tidy(p) for p in paras) if p]


def render(paragraphs: list[str], cfg: dict, clean: bool = False) -> str:
    if clean:
        paragraphs = [c for c in (clean_text(p, cfg) for p in paragraphs) if c]
    return "\n\n".join(paragraphs)
