"""Job runner.

One background worker, a queue, and a checkpoint file per job. The checkpoint
is the point of this module: on a two-hour recording, dying at minute 95 and
starting over is the difference between a tool you use and one you abandon.
It is only reachable because `retry` re-queues the *same* job id, and so lands
on the same directory — a fresh submission would get a fresh uuid and a fresh
empty checkpoint, which is the same as not having one.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import audio, backends, clean
from .backends import Segment, build_backend
from .settings import initial_prompt, load_config, output_folder

WORK_DIR = Path.home() / ".scribe" / "jobs"


class Cancelled(Exception):
    """Raised inside the worker when a job is deleted mid-run."""


@dataclass
class Job:
    id: str
    name: str
    source: Path
    preset_key: str
    preset: dict
    status: str = "queued"          # queued | running | done | failed
    stage: str = ""
    chunks_done: int = 0
    chunks_total: int = 0
    duration: float = 0.0
    speech: float = 0.0             # seconds actually sent to the model
    error: str = ""
    cancelled: bool = False
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    segments: list[Segment] = field(default_factory=list)
    breaks: set[int] = field(default_factory=set)   # segment indices opening a paragraph
    written: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return WORK_DIR / self.id

    # -- persistence ---------------------------------------------------------
    # The job list used to live only in memory, so restarting the server threw
    # away every transcript you could still read — and made the checkpoint
    # unreachable, since nothing was left to press Resume on.

    def save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "job.json").write_text(json.dumps({
                "id": self.id, "name": self.name, "source": str(self.source),
                "preset_key": self.preset_key, "preset": self.preset,
                "status": self.status, "duration": self.duration,
                "speech": self.speech, "chunks_done": self.chunks_done,
                "chunks_total": self.chunks_total, "error": self.error,
                "created": self.created, "written": self.written,
                "segments": [vars(s) for s in self.segments],
                "breaks": sorted(self.breaks),
            }))
        except OSError:
            pass        # never fail a transcription over its own bookkeeping

    @classmethod
    def load(cls, path: Path) -> "Job | None":
        try:
            d = json.loads(path.read_text())
            job = cls(id=d["id"], name=d["name"], source=Path(d["source"]),
                      preset_key=d["preset_key"], preset=d["preset"])
        except (OSError, json.JSONDecodeError, KeyError):
            return None

        for field_name in ("status", "duration", "speech", "chunks_done",
                           "chunks_total", "error", "created", "written"):
            if field_name in d:
                setattr(job, field_name, d[field_name])
        job.segments = [Segment(**s) for s in d.get("segments", [])]
        job.breaks = set(d.get("breaks", []))

        # Anything mid-flight when the server stopped isn't running any more.
        # Mark it resumable rather than leaving a progress bar that never moves.
        if job.status in ("running", "queued"):
            job.status = "failed"
            job.error = "Interrupted — press Resume to pick up where it stopped"
        return job

    @property
    def progress(self) -> float:
        if self.status == "done":
            return 1.0
        if not self.chunks_total:
            return 0.0
        return self.chunks_done / self.chunks_total

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "preset": self.preset_key,
            "model": self.preset.get("label", self.preset_key),
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "chunks_done": self.chunks_done,
            "chunks_total": self.chunks_total,
            "duration": round(self.duration, 1),
            "speech": round(self.speech, 1),
            # n breaks divide the text into n+1 paragraphs.
            "paragraphs": (len(self.breaks) + 1) if self.segments else 0,
            "error": self.error,
            "created": self.created,
            "written": self.written,
            "words": sum(len(s.text.split()) for s in self.segments),
        }


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._restore()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def _restore(self) -> None:
        """Reload past jobs from disk so the list survives a restart."""
        for path in sorted(WORK_DIR.glob("*/job.json")):
            job = Job.load(path)
            if job is not None:
                self._jobs[job.id] = job

    # -- public api ----------------------------------------------------------

    def submit(self, name: str, source: Path, preset_key: str, preset: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], name=name, source=source,
                  preset_key=preset_key, preset=preset)
        job.dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        job.save()
        self._queue.put(job.id)
        return job

    def retry(self, job_id: str) -> Job | None:
        """Re-queue an existing job. Same id, same directory, same checkpoint —
        so a failed two-hour rant resumes at the chunk it died on."""
        job = self.get(job_id)
        if job is None or job.status == "running":
            return None
        job.status = "queued"
        job.error = ""
        job.cancelled = False
        job.segments = []
        job.breaks = set()
        job.dir.mkdir(parents=True, exist_ok=True)
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        # Signal the worker before the caller deletes the directory out from
        # under it; the chunk loop checks this between chunks.
        job.cancelled = True
        return True

    # -- worker --------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            # A timeout rather than a blocking get: the wake-up doubles as the
            # idle tick that unloads the model. The menu bar app keeps this
            # process alive for days, so "nothing queued" needs to mean
            # something. No extra thread, and no cost while jobs are flowing.
            try:
                job_id = self._queue.get(timeout=60)
            except queue.Empty:
                self._release_idle_model()
                continue

            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._run(job)
                job.status = "done"
                job.stage = ""
            except Cancelled:
                job.status = "failed"
                job.error = "Cancelled"
            except Exception as exc:  # keep the worker alive across failures
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                job.save()
                self._queue.task_done()

    def _release_idle_model(self) -> None:
        minutes = load_config()["transcription"].get("unload_after_minutes", 0)
        if minutes and backends.release_idle(minutes * 60):
            print(f"scribe: unloaded idle model after {minutes} min", flush=True)

    def _run(self, job: Job) -> None:
        job.status = "running"
        cfg = load_config()
        ch = cfg["chunking"]

        job.stage = "decoding"
        wav = audio.to_wav(job.source, job.dir / "audio.wav",
                           normalize=ch.get("normalize", True))
        job.duration = audio.duration(wav)

        job.stage = "finding pauses"
        def plan(noise_db: float):
            sil = audio.detect_silences(
                wav, noise_db=noise_db, min_silence=ch["min_silence"]
            )
            return audio.plan_chunks(
                job.duration, sil,
                max_len=ch["max_chunk"],
                paragraph_gap=ch["paragraph_gap"],
                pad=ch["pad"],
            )

        chunks = plan(ch["noise_db"])

        # A recording with no speech at all is nearly always a threshold that is
        # too high for this microphone, not an actually silent room. Retry once
        # with the floor dropped before believing it — the alternative is
        # writing an empty transcript and saying nothing, which is how a real
        # 13-second note was silently thrown away.
        if not chunks and job.duration >= 1.0:
            fallback = ch["noise_db"] - 15.0
            chunks = plan(fallback)
            if chunks:
                print(f"scribe: no speech at {ch['noise_db']} dB, "
                      f"recovered at {fallback} dB", flush=True)

        if not chunks:
            raise audio.AudioError(
                f"No speech found in {job.duration:.1f}s of audio. The recording "
                f"may be silent, or the input level too low — check the mic input "
                f"in System Settings, or lower noise_db in scribe/config.toml."
            )

        job.chunks_total = len(chunks)
        job.speech = sum(c.length for c in chunks)

        checkpoint_path = job.dir / "checkpoint.json"
        done: dict[str, list[dict]] = {}
        if checkpoint_path.exists():
            try:
                done = json.loads(checkpoint_path.read_text())
            except json.JSONDecodeError:
                done = {}

        backend = build_backend(job.preset)
        language = job.preset.get("language")
        prompt = initial_prompt(cfg)
        # The model is fetched lazily on the first transcribe call, and
        # large-v3 is ~3 GB. Without saying so, the first run on a new model
        # sits at "0 of N" for several minutes and looks hung.
        job.stage = f"{backend.name} · loading model"

        collected: list[Segment] = []
        breaks: set[int] = set()
        for chunk in chunks:
            if job.cancelled:
                raise Cancelled()

            # A pause long enough to think in is a paragraph boundary, and the
            # planner already knows where they are.
            if chunk.para_break and collected:
                breaks.add(len(collected))

            key = str(chunk.index)
            if key in done:
                cached = [Segment(**s) for s in done[key]]
            else:
                piece = audio.slice_wav(wav, chunk, job.dir / f"chunk-{chunk.index:04d}.wav")
                try:
                    raw = backend.transcribe(piece, language=language, prompt=prompt)
                finally:
                    piece.unlink(missing_ok=True)
                cached = [s.shifted(chunk.start) for s in raw]
                done[key] = [vars(s) for s in cached]
                checkpoint_path.write_text(json.dumps(done))
                job.stage = backend.name        # model is resident from here on

            collected.extend(cached)
            job.chunks_done = chunk.index + 1
            job.segments = collected

        job.segments, job.breaks = _drop_repeats(collected, breaks)

        job.stage = "writing"
        job.written = _write_markdown(job, cfg)

        # The decoded PCM is ~115 MB/hour and reproducible from the original in
        # seconds. The source recording stays for playback, and every timestamp
        # above is valid against it because decoding never shifts time.
        wav.unlink(missing_ok=True)


def _drop_repeats(
    segments: list[Segment],
    breaks: set[int] | None = None,
    window: int = 3,
    min_chars: int = 25,
) -> tuple[list[Segment], set[int]]:
    """Discard a segment whose text just repeated.

    Whisper's classic long-file failure is the decoder latching onto a phrase
    and emitting it forever. Temperature fallback catches most of it inside the
    model; this catches what leaks through across chunk boundaries, where the
    model can't see that it's repeating itself.

    The length floor matters more than it looks. Without it this eats ordinary
    speech: "Yeah." twice in a conversation is two real utterances, and thinking
    out loud is full of repeated short beats — "Right." "Okay." "So." A runaway
    decode loop is always a long phrase, so only long repeats are suspicious.

    Paragraph break indices are remapped as segments are dropped, so the
    structure survives the filter.
    """
    breaks = breaks or set()
    out: list[Segment] = []
    moved: set[int] = set()
    pending_break = False

    for i, seg in enumerate(segments):
        if i in breaks:
            pending_break = True

        text = seg.text.strip().lower()
        recent = [s.text.strip().lower() for s in out[-window:]]
        if text and len(text) >= min_chars and text in recent:
            continue        # a real repetition loop

        if pending_break and out:
            moved.add(len(out))
            pending_break = False
        out.append(seg)

    return out, moved


# --- export ------------------------------------------------------------------

def _timecode(seconds: float, sep: str = ",") -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _slug(name: str) -> str:
    stem = Path(name).stem.strip() or "rant"
    return re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower() or "rant"


def _front_matter(job: Job, kind: str) -> str:
    return (
        "---\n"
        f"title: {job.name}\n"
        f"recorded: {job.created}\n"
        f"duration: {_timecode(job.duration, '.')[:8]}\n"
        f"speech: {_timecode(job.speech, '.')[:8]}\n"
        f"model: {job.preset.get('label', job.preset_key)}\n"
        f"version: {kind}\n"
        f"audio: {job.source}\n"
        "---\n\n"
    )


def export(job: Job, fmt: str) -> str:
    cfg = load_config()
    paras = clean.to_paragraphs(job.segments, job.breaks)

    if fmt == "txt":
        return clean.render(paras, cfg, clean=True) + "\n"

    if fmt == "md":
        return _front_matter(job, "cleaned") + clean.render(paras, cfg, clean=True) + "\n"

    if fmt == "raw":
        return _front_matter(job, "verbatim") + clean.render(paras, cfg, clean=False) + "\n"

    if fmt == "srt":
        blocks = [
            f"{i}\n{_timecode(s.start)} --> {_timecode(s.end)}\n{s.text}\n"
            for i, s in enumerate(job.segments, 1)
        ]
        return "\n".join(blocks)

    if fmt == "vtt":
        blocks = [
            f"{_timecode(s.start, '.')} --> {_timecode(s.end, '.')}\n{s.text}\n"
            for s in job.segments
        ]
        return "WEBVTT\n\n" + "\n".join(blocks)

    raise ValueError(f"Unknown export format: {fmt}")


def _write_markdown(job: Job, cfg: dict) -> list[str]:
    """Drop both versions into the output folder the moment a rant finishes.

    A transcript that needs a manual export step is a transcript you stop
    collecting. Written as a pair so the cleaning can be aggressive: the raw
    file means nothing is ever actually lost.
    """
    try:
        folder = output_folder(cfg)
    except OSError as exc:
        job.error = f"Transcribed, but could not write to output folder: {exc}"
        return []

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    base = f"{stamp}-{_slug(job.name)}"
    written = []
    for suffix, fmt in ((".md", "md"), (".raw.md", "raw")):
        path = folder / f"{base}{suffix}"
        path.write_text(export(job, fmt), encoding="utf-8")
        written.append(str(path))
    return written
