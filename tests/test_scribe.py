"""Unit tests for the pure logic: chunk planning, cleaning, timecodes.

Everything here runs without ffmpeg or a model, so it stays fast enough to run
on every edit.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scribe import backends, clean                          # noqa: E402
from scribe.audio import gain_db, plan_chunks, speech_regions  # noqa: E402
from scribe.backends import Segment                         # noqa: E402
from scribe.jobs import _drop_repeats, _slug, _timecode     # noqa: E402

CFG = {
    "cleaning": {
        "always": ["um", "uh", "erm", "hmm", "ah"],
        "phrases": ["you know", "i mean"],
        "aggressive": [],
        "fix_stutters": True,
    }
}


# --- speech regions ----------------------------------------------------------

def test_regions_invert_silence():
    assert speech_regions(10.0, [(4.0, 6.0)], pad=0.0) == [(0.0, 4.0), (6.0, 10.0)]


def test_regions_drop_slivers():
    # A 50 ms blip between two pauses is not speech worth a model call.
    assert speech_regions(10.0, [(0.0, 4.0), (4.05, 10.0)], pad=0.0) == []


def test_regions_pad_but_stay_in_bounds():
    (start, end), = speech_regions(10.0, [], pad=0.25)
    assert (start, end) == (0.0, 10.0)      # padding never runs off either end


# --- chunk planning ----------------------------------------------------------

def test_long_pause_is_skipped_not_transcribed():
    """The regression that motivated all of this.

    Speech, a 45 s think, more speech. The old planner clock-cut straight
    through the pause and handed Whisper 25 s of pure room tone — the exact
    input it hallucinates "Thanks for watching!" on.
    """
    chunks = plan_chunks(133.0, [(30.0, 75.0), (105.0, 113.0)])
    for c in chunks:
        overlap = max(0.0, min(c.end, 75.0) - max(c.start, 30.0))
        assert overlap < 1.0, f"chunk {c.index} sits inside the 45 s pause"


def test_thinking_pause_opens_a_paragraph():
    chunks = plan_chunks(133.0, [(30.0, 75.0), (105.0, 113.0)])
    breaks = [c.index for c in chunks if c.para_break]
    assert len(breaks) == 2                  # one per long pause, not the first chunk
    assert chunks[0].para_break is False


def test_short_gaps_do_not_split_paragraphs():
    # Half-second breaths are speech rhythm, not thinking.
    chunks = plan_chunks(20.0, [(5.0, 5.4), (10.0, 10.5)])
    assert not any(c.para_break for c in chunks)


def test_chunks_never_exceed_the_window():
    chunks = plan_chunks(300.0, [])
    assert chunks and all(c.length <= 25.0 + 1e-6 for c in chunks)


def test_long_speech_splits_evenly_not_into_orphans():
    """30 s of unbroken speech should be 2x15 s, not 25 s + a 5 s stub that
    costs a whole model invocation for almost nothing."""
    chunks = plan_chunks(30.0, [])
    assert len(chunks) == 2
    assert all(abs(c.length - 15.0) < 0.01 for c in chunks)


def test_timestamps_stay_on_the_original_timeline():
    # Required for playback: excising silence must not renumber time.
    chunks = plan_chunks(133.0, [(30.0, 75.0), (105.0, 113.0)])
    assert chunks[-1].end == pytest.approx(133.0, abs=0.3)


def test_silence_only_input_plans_nothing():
    assert plan_chunks(60.0, [(0.0, 60.0)]) == []


def test_empty_input():
    assert plan_chunks(0.0, []) == []


# --- level normalisation -----------------------------------------------------

def test_gain_lifts_a_quiet_recording():
    """The bug this exists for: AVAudioRecorder captured at -56 dB mean, which
    put an entire real recording under the silence threshold."""
    assert gain_db(-56.0, -32.0) == pytest.approx(31.0)


def test_gain_never_clips():
    """Peak headroom wins when raising the mean would push the peak over."""
    assert gain_db(-40.0, -2.0) == pytest.approx(1.0)     # not the +20 the mean wants


def test_gain_attenuates_a_hot_recording():
    assert gain_db(-10.0, -1.0) == pytest.approx(-10.0)


def test_gain_leaves_a_good_recording_alone():
    """A browser recording is already well levelled; barely touch it."""
    assert abs(gain_db(-22.0, -0.2)) < 1.5


def test_gain_is_capped_for_near_silence():
    """Never amplify a genuinely silent file into pure noise."""
    assert gain_db(-90.0, -80.0) == pytest.approx(40.0)


# --- cleaning ----------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("Um, so I was thinking", "So I was thinking"),
    ("I was, uh, thinking about it", "I was thinking about it"),
    ("the the thing is", "The thing is"),
    ("It was, you know, fine", "It was fine"),
    ("Hmm. Ah, right.", "Right."),
])
def test_clean_strips_filler(raw, want):
    assert clean.clean_text(raw, CFG) == want


@pytest.mark.parametrize("sentence", [
    "The build is kind of broken.",
    "It sort of works on my machine.",
    "Do you know the answer?",
    "What I mean is the API changed.",
    "You know the drill.",
    "Kind of.",
])
def test_phrases_used_as_grammar_survive(sentence):
    """Regression. Phrases were stripped wherever they appeared, although the
    config promised only as asides — so "Do you know the answer?" came out as
    "Do the answer?" and "kind of broken" silently lost its hedge. A phrase with
    no comma beside it is part of the sentence."""
    cfg = {"cleaning": {"always": [], "phrases": ["you know", "i mean", "sort of", "kind of"],
                        "aggressive": [], "fix_stutters": True}}
    assert clean.clean_text(sentence, cfg) == sentence


@pytest.mark.parametrize("raw, want", [
    ("So, you know, I tried it.", "So I tried it."),     # between commas
    ("I mean, that was the plan.", "That was the plan."), # opens a sentence
    ("It was bad, you know.", "It was bad."),             # trailing tag
    ("Is it done, sort of?", "Is it done?"),
])
def test_phrases_used_as_asides_are_removed(raw, want):
    cfg = {"cleaning": {"always": [], "phrases": ["you know", "i mean", "sort of", "kind of"],
                        "aggressive": [], "fix_stutters": True}}
    assert clean.clean_text(raw, cfg) == want


def test_clean_leaves_meaningful_words_alone():
    text = "I actually like the literal basically-correct version"
    assert clean.clean_text(text, CFG) == text


def test_clean_is_idempotent():
    once = clean.clean_text("Um, so, uh, the the point", CFG)
    assert clean.clean_text(once, CFG) == once


def test_paragraphs_split_on_breaks():
    segs = [Segment(0, 1, "one"), Segment(1, 2, "two"), Segment(2, 3, "three")]
    assert clean.to_paragraphs(segs, {2}) == ["One two", "Three"]


def test_paragraphs_ignore_a_leading_break():
    segs = [Segment(0, 1, "one"), Segment(1, 2, "two")]
    assert clean.to_paragraphs(segs, {0}) == ["One two"]


# --- repeat filter -----------------------------------------------------------

def test_short_repeats_survive():
    """Thinking out loud is full of repeated short beats. Deleting them was the
    old filter's bug: two "Yeah."s are two real utterances, not a decode loop."""
    convo = ["Yeah.", "So what I mean is", "Yeah.", "Right.", "Okay.", "Right."]
    kept, _ = _drop_repeats([Segment(i, i + 1, t) for i, t in enumerate(convo)])
    assert [s.text for s in kept] == convo


def test_long_repeats_are_dropped():
    loop = "Thanks for watching this video, don't forget to subscribe."
    segs = [Segment(i, i + 1, loop) for i in range(4)]
    kept, _ = _drop_repeats(segs)
    assert len(kept) == 1


def test_breaks_are_remapped_when_segments_drop():
    loop = "Thanks for watching this video, don't forget to subscribe."
    segs = [Segment(0, 1, loop), Segment(1, 2, loop), Segment(2, 3, "A new thought.")]
    kept, breaks = _drop_repeats(segs, breaks={2})
    assert len(kept) == 2 and breaks == {1}      # break followed its segment


# --- model eviction ----------------------------------------------------------

def test_release_idle_keeps_a_recently_used_model():
    backends._CACHE["k"] = object()
    backends._LAST_USED = time.monotonic()
    try:
        assert backends.release_idle(60) is False
        assert backends._CACHE                      # still resident
    finally:
        backends._CACHE.clear()


def test_release_idle_frees_after_the_threshold():
    backends._CACHE["k"] = object()
    backends._LAST_USED = time.monotonic() - 120
    assert backends.release_idle(60) is True
    assert not backends._CACHE


def test_release_idle_disabled_by_zero():
    """0 means "keep it resident forever" — the escape hatch in config.toml."""
    backends._CACHE["k"] = object()
    backends._LAST_USED = time.monotonic() - 10_000
    try:
        assert backends.release_idle(0) is False
        assert backends._CACHE
    finally:
        backends._CACHE.clear()


# --- formatting --------------------------------------------------------------

def test_timecode():
    assert _timecode(0) == "00:00:00,000"
    assert _timecode(3661.5) == "01:01:01,500"
    assert _timecode(59.999, ".") == "00:00:59.999"


def test_slug():
    assert _slug("Recording 2026-09-03 14:42.webm") == "recording-2026-09-03-14-42"
    assert _slug("") == "rant"
