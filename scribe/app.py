"""Local web server.

Deliberately boring: the browser polls for job state once a second rather than
holding a socket open. On a single-user tool running on localhost, polling has
no downside and one real upside — nothing to reconnect when a job blocks the
worker thread for thirty seconds.
"""

from __future__ import annotations

import os
import shutil
import signal
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import audio, clean
from .jobs import JobStore, export
from .settings import load_config, load_presets, output_folder

BASE = Path(__file__).parent
UPLOADS = Path.home() / ".scribe" / "uploads"

# MediaRecorder's container depends on the browser: Chrome gives WebM, Safari
# gives MP4. The extension has to follow, because appending MP4 fragments the
# way you can append WebM clusters does not produce a decodable file.
_CONTAINERS = {"webm": ".webm", "ogg": ".ogg", "mp4": ".mp4", "mpeg": ".mp3", "wav": ".wav"}

store = JobStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    audio.check_tools()
    output_folder()          # fail now if the transcript folder isn't writable
    yield


app = FastAPI(title="scribe", lifespan=lifespan)

# Archivo, bundled rather than pulled from fonts.googleapis.com. That link was
# the only third-party request the app made; serving the files locally is what
# makes "nothing leaves this machine" literally true and lets the UI work with
# no network at all. Mounted narrowly on the fonts directory rather than the
# whole of static/, which is already served through the index route.
app.mount("/fonts", StaticFiles(directory=BASE / "static" / "fonts"), name="fonts")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/presets")
def presets() -> dict:
    return {
        key: {"label": val.get("label", key), "backend": val.get("backend")}
        for key, val in load_presets().items()
    }


@app.get("/api/config")
def config() -> dict:
    cfg = load_config()
    return {"folder": str(Path(cfg["output"]["folder"]).expanduser())}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [j.as_dict() for j in store.all()]}


@app.post("/api/quit")
def quit_server() -> dict:
    """Stop the server from the UI, so it doesn't need a terminal to shut down.

    Safe to press mid-transcription: each chunk is checkpointed as it lands and
    the job record persists, so an interrupted job reappears marked Resume and
    carries on from where it stopped rather than re-running the whole file.
    """
    interrupted = [j.name for j in store.all() if j.status in ("running", "queued")]

    def _stop() -> None:
        time.sleep(0.4)                        # let the response reach the browser
        os.kill(os.getpid(), signal.SIGINT)    # same path ctrl-c takes: graceful

    threading.Thread(target=_stop, daemon=True).start()
    return {"stopping": True, "interrupted": interrupted}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not store.remove(job_id):
        raise HTTPException(404, "No such job")
    # remove() has flagged the worker; it checks between chunks, so the tree is
    # no longer being written into by the time this lands.
    if job and job.dir.exists():
        shutil.rmtree(job.dir, ignore_errors=True)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    job = store.retry(job_id)
    if job is None:
        raise HTTPException(404, "No such job, or it is already running")
    return job.as_dict()


@app.get("/api/jobs/{job_id}/transcript")
def transcript(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return {
        "segments": [vars(s) for s in job.segments],
        "breaks": sorted(job.breaks),
    }


@app.get("/api/jobs/{job_id}/text")
def job_text(job_id: str, format: str = "md") -> dict:
    """Paragraphs as JSON, for reading in the browser.

    Separate from /export because that returns a file with front matter and a
    download header. This is the same text with the packaging removed.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")

    paragraphs = clean.to_paragraphs(job.segments, job.breaks)
    if format == "md":
        cfg = load_config()
        paragraphs = [p for p in (clean.clean_text(x, cfg) for x in paragraphs) if p]
    return {
        "paragraphs": paragraphs,
        "words": sum(len(p.split()) for p in paragraphs),
    }


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str, format: str = "md") -> PlainTextResponse:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    try:
        body = export(job, format)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    stem = Path(job.name).stem or "transcript"
    ext = "raw.md" if format == "raw" else format
    return PlainTextResponse(
        body,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'},
    )


def _submit(path: Path, name: str, preset_key: str):
    all_presets = load_presets()
    if preset_key not in all_presets:
        raise HTTPException(400, f"Unknown preset '{preset_key}'")
    return store.submit(name, path, preset_key, all_presets[preset_key]).as_dict()


@app.post("/api/upload")
async def upload(preset: str, file: UploadFile = File(...)) -> dict:
    original = Path(file.filename or "audio").name
    # Prefix rather than overwrite: two files called audio.m4a would otherwise
    # collide, and the second would replace the source a queued job points at.
    dest = UPLOADS / f"{uuid.uuid4().hex[:8]}-{original}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    return _submit(dest, original, preset)


# --- browser recording -------------------------------------------------------
# MediaRecorder emits a stream of blobs. Everything after the first is a
# continuation of the same container, so appending them in order to one file is
# the whole trick. Streaming them up as they arrive means a three-hour session
# never accumulates in the tab's memory.

def _rec_path(rec_id: str, ext: str = "") -> Path:
    safe = "".join(c for c in rec_id if c.isalnum())
    if not safe:
        raise HTTPException(400, "Bad recording id")
    if ext:
        return UPLOADS / f"rec-{safe}{ext}"
    found = sorted(UPLOADS.glob(f"rec-{safe}.*"))
    if not found:
        raise HTTPException(404, "Nothing was recorded")
    return found[0]


@app.post("/api/record/{rec_id}/chunk")
async def record_chunk(rec_id: str, request: Request) -> dict:
    # e.g. "audio/webm;codecs=opus" -> ".webm"
    mime = (request.headers.get("content-type") or "").split(";")[0].strip()
    subtype = mime.rsplit("/", 1)[-1].lower()
    target = _rec_path(rec_id, _CONTAINERS.get(subtype, ".webm"))

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("ab") as fh:
        fh.write(await request.body())
    return {"bytes": target.stat().st_size}


@app.post("/api/record/{rec_id}/finish")
def record_finish(rec_id: str, preset: str, name: str = "") -> dict:
    target = _rec_path(rec_id)
    return _submit(target, name or target.stem, preset)
