"""Model backends.

Every engine reduces to the same contract: hand it a 16 kHz mono wav, get back
timestamped segments. That's the whole abstraction, and it's what makes
"models of your choice" a config line instead of a rewrite.

Backends are constructed lazily and cache their loaded model, because loading
large-v3-turbo takes seconds and a long file means many chunks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class Segment:
    start: float
    end: float
    text: str

    def shifted(self, offset: float) -> "Segment":
        return Segment(self.start + offset, self.end + offset, self.text)


class BackendError(RuntimeError):
    pass


class Backend(Protocol):
    name: str

    def transcribe(self, wav: Path, language: str | None = None,
                   prompt: str | None = None) -> list[Segment]:
        ...


# --- whisper.cpp -------------------------------------------------------------

class WhisperCppBackend:
    """Subprocess wrapper around whisper-cli.

    Slower to start than the in-process backends (it reloads the model on every
    call) but it has no Python dependencies at all and runs any GGML file,
    including community fine-tunes.
    """

    def __init__(self, model: str, binary: str = "whisper-cli", threads: int = 8,
                 extra_args: list[str] | None = None):
        self.name = f"whisper.cpp:{Path(model).stem}"
        self.model = str(Path(model).expanduser())
        self.binary = shutil.which(binary) or binary
        self.threads = threads
        self.extra_args = extra_args or []

        if not Path(self.model).exists():
            raise BackendError(f"Model file not found: {self.model}")
        if not shutil.which(self.binary):
            raise BackendError(
                f"'{binary}' not on PATH. Install with: brew install whisper-cpp"
            )

    def transcribe(self, wav: Path, language: str | None = None,
                   prompt: str | None = None) -> list[Segment]:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "out"
            cmd = [
                self.binary,
                "-m", self.model,
                "-f", str(wav),
                "-t", str(self.threads),
                "-oj", "-of", str(prefix),
                "--no-prints",
                # Stops a bad chunk from poisoning the ones after it. On long
                # files this is the single most valuable flag here.
                "--no-context",
            ]
            if language:
                cmd += ["-l", language]
            if prompt:
                cmd += ["--prompt", prompt]
            cmd += self.extra_args

            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise BackendError(f"whisper-cli failed: {(proc.stderr or '').strip()[:300]}")

            payload = json.loads((prefix.with_suffix(".json")).read_text())

        segments = []
        for item in payload.get("transcription", []):
            offsets = item.get("offsets") or {}
            text = (item.get("text") or "").strip()
            if not text:
                continue
            segments.append(Segment(
                start=float(offsets.get("from", 0)) / 1000.0,
                end=float(offsets.get("to", 0)) / 1000.0,
                text=text,
            ))
        return segments


# --- mlx-whisper -------------------------------------------------------------

class MlxWhisperBackend:
    """Apple MLX build of Whisper. Fastest Whisper option on Apple Silicon."""

    def __init__(self, model: str):
        self.name = f"mlx-whisper:{model.split('/')[-1]}"
        self.repo = model
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as exc:
            raise BackendError("mlx-whisper not installed. pip install mlx-whisper") from exc

    def transcribe(self, wav: Path, language: str | None = None,
                   prompt: str | None = None) -> list[Segment]:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=self.repo,
            language=language,
            # Whisper imitates the style of whatever it's primed with. Seeded
            # with a deliberately disfluent sentence it stops silently tidying
            # ums and false starts as it decodes — which is the only reason a
            # genuinely verbatim version is possible. The same prompt carries a
            # vocabulary list, which fixes the names it would otherwise mangle.
            initial_prompt=prompt,
            # Same reasoning as --no-context above: each chunk stands alone, so
            # one hallucinated segment can't cascade through the rest.
            condition_on_previous_text=False,
            # Keep the temperature-fallback ladder. When a decode comes back
            # with a suspiciously repetitive compression ratio or a low average
            # log-probability, Whisper retries hotter. This is the main defence
            # against repetition loops.
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            verbose=None,
        )
        return [
            Segment(float(s["start"]), float(s["end"]), (s.get("text") or "").strip())
            for s in result.get("segments", [])
            if (s.get("text") or "").strip()
        ]


# --- parakeet-mlx ------------------------------------------------------------

class ParakeetMlxBackend:
    """NVIDIA Parakeet via MLX. English only, but very fast and very accurate.

    NOTE: parakeet-mlx's API has moved around more than the others. The result
    shape is read defensively below; if a release changes it, this is the one
    place to patch.
    """

    def __init__(self, model: str):
        self.name = f"parakeet:{model.split('/')[-1]}"
        self.repo = model
        self._model = None
        try:
            import parakeet_mlx  # noqa: F401
        except ImportError as exc:
            raise BackendError("parakeet-mlx not installed. pip install parakeet-mlx") from exc

    def _load(self):
        if self._model is None:
            from parakeet_mlx import from_pretrained
            self._model = from_pretrained(self.repo)
        return self._model

    def transcribe(self, wav: Path, language: str | None = None,
                   prompt: str | None = None) -> list[Segment]:
        # Parakeet is a CTC/TDT model with no prompt conditioning, so `prompt`
        # is accepted and ignored rather than pretended at. It is also more
        # literal than Whisper by nature, which makes it the better raw pass if
        # you ever want one.
        result = self._load().transcribe(str(wav))

        parts = getattr(result, "sentences", None) or getattr(result, "segments", None)
        if parts:
            out = []
            for p in parts:
                text = (getattr(p, "text", "") or "").strip()
                if text:
                    out.append(Segment(
                        float(getattr(p, "start", 0.0)),
                        float(getattr(p, "end", 0.0)),
                        text,
                    ))
            return out

        # Fall back to a single untimed blob rather than losing the transcript.
        text = (getattr(result, "text", "") or "").strip()
        return [Segment(0.0, 0.0, text)] if text else []


# --- construction ------------------------------------------------------------

_REGISTRY = {
    "whisper-cpp": WhisperCppBackend,
    "mlx-whisper": MlxWhisperBackend,
    "parakeet-mlx": ParakeetMlxBackend,
}

_CACHE: dict[str, Backend] = {}
_LAST_USED: float = 0.0


def _release_mlx() -> None:
    """Drop the weights mlx-whisper is holding, and the Metal buffers behind them.

    Clearing ``_CACHE`` alone frees nothing on this backend: mlx_whisper keeps
    the loaded model on ``transcribe.ModelHolder.model``, a class attribute of
    its own, so our instance going away leaves ~1.6 GB resident. Both have to
    be released, then MLX's buffer pool returned to the OS.

    Looked up through ``sys.modules`` on purpose — if mlx was never imported
    there is nothing to free, and importing it here would be the opposite of
    the point.
    """
    holder = getattr(sys.modules.get("mlx_whisper.transcribe"), "ModelHolder", None)
    if holder is not None:
        holder.model = None
        holder.model_path = None

    mx = sys.modules.get("mlx.core")
    if mx is not None and hasattr(mx, "clear_cache"):
        mx.clear_cache()


def release_idle(after_seconds: float) -> bool:
    """Unload cached models if none has been used for ``after_seconds``.

    Matters because the menu bar app keeps the server alive indefinitely. Left
    alone, the first transcription of the day pins the model in memory until
    you quit. Reloading costs a few seconds on the next job, which is invisible
    next to the recording that precedes it.

    Returns True when something was actually released.
    """
    if after_seconds <= 0 or not _CACHE:
        return False
    if time.monotonic() - _LAST_USED < after_seconds:
        return False

    _CACHE.clear()
    _release_mlx()
    return True


def build_backend(preset: dict) -> Backend:
    kind = preset.get("backend")
    if kind not in _REGISTRY:
        raise BackendError(
            f"Unknown backend '{kind}'. Choose one of: {', '.join(_REGISTRY)}"
        )

    global _LAST_USED
    _LAST_USED = time.monotonic()

    key = json.dumps(preset, sort_keys=True)
    if key not in _CACHE:
        opts = {k: v for k, v in preset.items() if k not in {"backend", "label", "language"}}
        _CACHE[key] = _REGISTRY[kind](**opts)
    return _CACHE[key]
