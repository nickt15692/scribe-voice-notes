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


def _strip_tokens(text: str, words: list[str]) -> str:
    """Delete each listed word or phrase, absorbing the punctuation it strands."""
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
    text = _strip_tokens(text, rules.get("always") or [])
    text = _strip_tokens(text, rules.get("phrases") or [])
    text = _strip_tokens(text, rules.get("aggressive") or [])
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
