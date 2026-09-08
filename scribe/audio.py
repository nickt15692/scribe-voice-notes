"""Audio ingest and silence-aware chunk planning.

Everything upstream of the model lives here: decode whatever the user gave us
into 16 kHz mono PCM (Whisper's native format, so no resample happens inside
the model), find the pauses, and decide where to cut.

Cutting on silence rather than on a fixed clock matters more than it looks.
Whisper is heavily context-dependent, and a chunk boundary dropped in the
middle of a word costs you that word plus the model's bearings on either side
of the seam.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise AudioError(f"{Path(cmd[0]).name} failed: {' / '.join(tail)}")
    return proc


def check_tools() -> None:
    """Fail loudly at startup rather than three minutes into a job."""
    missing = [n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not shutil.which(p)]
    if missing:
        raise AudioError(
            f"{' and '.join(missing)} not found on PATH. Install with: brew install ffmpeg"
        )


def duration(path: Path) -> float:
    proc = _run([
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise AudioError(f"Could not read duration of {path.name}") from exc


_MEAN_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")
_MAX_RE = re.compile(r"max_volume:\s*(-?[\d.]+) dB")


def measure_levels(path: Path) -> tuple[float, float] | None:
    """Return (mean dBFS, peak dBFS) from ffmpeg's volumedetect, or None."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    err = proc.stderr or ""
    mean, peak = _MEAN_RE.search(err), _MAX_RE.search(err)
    if not mean or not peak:
        return None
    return float(mean.group(1)), float(peak.group(1))


def gain_db(mean: float, peak: float, target_mean: float = -20.0,
            ceiling: float = -1.0, limit: float = 40.0) -> float:
    """How much to scale a recording so downstream thresholds mean something.

    Different capture paths arrive at wildly different levels: the browser's
    getUserMedia applies automatic gain control, while AVAudioRecorder does not,
    so the same voice lands 15-30 dB quieter from the menu bar app than from a
    tab. With a fixed silence threshold that difference is the whole ballgame —
    a quiet recording gets classified as silence end to end and produces an
    empty transcript.

    Aim the *mean* at speech level, but never let the peak clip: the gain is
    whichever of those two constraints binds first. Because it's a single
    constant applied to the whole file, quiet passages stay proportionally
    quiet — pause structure survives, which a dynamic normaliser would destroy
    by pumping the noise floor up during the silences we cut on.

    ``limit`` stops a near-silent recording being amplified into pure noise.
    """
    return max(-limit, min(limit, min(target_mean - mean, ceiling - peak)))


def to_wav(src: Path, dst: Path, normalize: bool = True) -> Path:
    """Decode anything ffmpeg understands into 16 kHz mono signed 16-bit PCM."""
    dst.parent.mkdir(parents=True, exist_ok=True)

    filters = []
    if normalize:
        levels = measure_levels(src)
        if levels:
            g = gain_db(*levels)
            if abs(g) >= 0.5:                 # below that it isn't worth a pass
                filters.append(f"volume={g:.1f}dB")

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn",                      # drop any video stream
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-c:a", "pcm_s16le",
    ]
    if filters:
        cmd += ["-af", ",".join(filters)]
    _run(cmd + [str(dst)])
    return dst


# --- silence detection -------------------------------------------------------

_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silences(
    wav: Path,
    noise_db: float = -35.0,
    min_silence: float = 0.45,
) -> list[tuple[float, float]]:
    """Return (start, end) spans that ffmpeg considers silent.

    This stands in for a proper VAD model. Silero would be more accurate, but it
    drags in torch for what is ultimately a yes/no question about loudness, and
    ffmpeg is already a hard dependency here.

    Raise ``noise_db`` toward 0 to treat quieter passages as silence; lower it
    (e.g. -45) for noisy rooms where the floor is high.
    """
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(wav),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # silencedetect writes to stderr and ffmpeg exits 0; parse leniently.
    spans: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (proc.stderr or "").splitlines():
        if m := _SIL_START.search(line):
            pending = float(m.group(1))
        elif m := _SIL_END.search(line):
            end = float(m.group(1))
            start = pending if pending is not None else max(0.0, end - min_silence)
            spans.append((start, end))
            pending = None
    return spans


# --- chunk planning ----------------------------------------------------------

@dataclass(frozen=True)
class Chunk:
    index: int
    start: float
    end: float
    para_break: bool = False    # a thinking pause preceded this chunk

    @property
    def length(self) -> float:
        return self.end - self.start


def speech_regions(
    total: float,
    silences: list[tuple[float, float]],
    pad: float = 0.25,
    min_speech: float = 0.15,
) -> list[tuple[float, float]]:
    """Invert the silence spans into the stretches that actually contain speech.

    Timestamps stay on the original timeline throughout — nothing is compressed
    or renumbered — so every offset the model reports still lines up with the
    recording you can play back.
    """
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start > cursor:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        regions.append((cursor, total))

    # Pad the edges so word onsets and trailing consonants survive the cut.
    return [
        (max(0.0, s - pad), min(total, e + pad))
        for s, e in regions
        if e - s >= min_speech
    ]


def _emit(chunks: list[Chunk], start: float, end: float, max_len: float, brk: bool) -> None:
    """Append one span, splitting evenly if it outruns the model's window.

    Splitting into equal parts rather than repeatedly shaving off ``max_len``
    avoids leaving a two-second orphan at the end, which would cost a whole
    model invocation for almost no audio.
    """
    span = end - start
    parts = max(1, math.ceil(span / max_len)) if span > max_len else 1
    step = span / parts
    for i in range(parts):
        chunks.append(Chunk(
            index=len(chunks),
            start=start + i * step,
            end=start + (i + 1) * step,
            para_break=brk and i == 0,
        ))


def plan_chunks(
    total: float,
    silences: list[tuple[float, float]],
    max_len: float = 25.0,
    paragraph_gap: float = 2.0,
    pad: float = 0.25,
) -> list[Chunk]:
    """Build chunks out of speech only, and mark where the thinking pauses were.

    The stock approach — cut the timeline at pause midpoints — is right for the
    half-second gaps between sentences and badly wrong for the long ones. A
    forty-second pause while you work something out gets sliced into chunks that
    are *entirely room tone*, and room tone is precisely the input Whisper
    hallucinates on: it was trained on subtitle tracks, so silence decodes to
    "Thanks for watching!". Skipping the silence removes the failure mode at the
    source, and as a side effect stops you paying to transcribe your own pauses.

    One threshold does two jobs. A gap under ``paragraph_gap`` is speech rhythm,
    so the audio either side stays in the same chunk. A gap over it means you
    stopped to think, which ends the chunk and starts a new paragraph — so the
    transcript comes out shaped like the thinking that produced it.

    ``max_len`` sits below Whisper's 30 s window on purpose: the model pads short
    input, so headroom is free, while overshooting is silently truncated.
    """
    chunks: list[Chunk] = []
    pending: list[tuple[float, float]] = []
    # False, not True: the opening chunk starts the first paragraph by virtue of
    # being first. Flagging it would insert a break before any text exists.
    opens_paragraph = False

    def flush(next_break: bool) -> None:
        nonlocal pending, opens_paragraph
        if pending:
            _emit(chunks, pending[0][0], pending[-1][1], max_len, opens_paragraph)
            pending = []
            opens_paragraph = next_break

    prev_end: float | None = None
    for start, end in speech_regions(total, silences, pad):
        if pending:
            if prev_end is not None and start - prev_end >= paragraph_gap:
                flush(True)                      # you stopped to think
            elif end - pending[0][0] > max_len:
                flush(False)                     # just out of room
        pending.append((start, end))
        prev_end = end
    flush(False)

    return chunks


def slice_wav(wav: Path, chunk: Chunk, dst: Path) -> Path:
    """Extract one chunk.

    ``-ss`` goes *before* ``-i`` so ffmpeg seeks the input instead of decoding
    and discarding everything ahead of the mark. On a two-hour rant the output-
    side form makes slicing quadratic — every chunk re-reads the file from zero.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{chunk.start:.3f}",
        "-t", f"{chunk.length:.3f}",
        "-i", str(wav),
        "-c", "copy",
        str(dst),
    ])
    return dst
