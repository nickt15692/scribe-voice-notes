"""Config loading.

Two files on purpose: `presets.toml` is which model to run, `config.toml` is
everything about how the app behaves. They change for different reasons and at
different times — you settle on a model and stop touching presets, while the
filler list and vocabulary get edited as you learn how you actually talk.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.toml"
PRESETS_PATH = BASE / "presets.toml"

_DEFAULTS: dict = {
    "output": {"folder": "~/Documents/scribe", "keep_audio": True},
    "chunking": {
        "normalize": True,
        "paragraph_gap": 2.0,
        "noise_db": -35.0,
        "min_silence": 0.30,
        "max_chunk": 25.0,
        "pad": 0.25,
    },
    "transcription": {"verbatim_prompt": "", "vocabulary": [], "unload_after_minutes": 10},
    "cleaning": {"always": [], "phrases": [], "aggressive": [], "fix_stutters": True},
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config() -> dict:
    """Read config.toml, falling back to defaults for anything absent.

    Deliberately tolerant: a missing or half-written config should degrade to
    working defaults rather than stop a recording from being transcribed.
    """
    if not CONFIG_PATH.exists():
        return _DEFAULTS
    try:
        with CONFIG_PATH.open("rb") as fh:
            return _merge(_DEFAULTS, tomllib.load(fh))
    except tomllib.TOMLDecodeError:
        return _DEFAULTS


def load_presets() -> dict:
    with PRESETS_PATH.open("rb") as fh:
        return tomllib.load(fh).get("presets", {})


def output_folder(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    folder = Path(cfg["output"]["folder"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def initial_prompt(cfg: dict | None = None) -> str | None:
    """Build the decoder prompt: a disfluent sample plus your vocabulary.

    Whisper imitates the style of whatever it's primed with, so the sample
    sentence pulls it toward verbatim instead of tidying as it decodes, and the
    word list biases spelling of names it would otherwise mangle.
    """
    cfg = cfg or load_config()
    tr = cfg["transcription"]
    parts = [tr.get("verbatim_prompt") or "", " ".join(tr.get("vocabulary") or [])]
    prompt = " ".join(p.strip() for p in parts if p.strip())
    return prompt or None
