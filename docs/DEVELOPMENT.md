# Development

Common changes, the conventions to follow, and the things that will bite you.

Read the [Gotchas](#gotchas) before changing `audio.py`, `jobs.py` or
`index.html`. Several of them are counter-intuitive and were found the hard way.

## Transcripts are private — ask first

**This rule comes before everything else in this document.**

If you are working on someone else's installation — as a collaborator or an AI
assistant — get the user's **explicit permission before reading any part of
their transcripts**. That includes a single line, the front matter, a word
count, or a quick spot check. Permission covers the specific thing you asked
about; ask again for anything beyond it.

It covers every route to transcript content, several of which are easy to miss:

| Route | Why it counts |
|---|---|
| Output folder `.md` / `.raw.md` | The transcripts themselves, front matter included |
| `~/.scribe/jobs/*/job.json`, `checkpoint.json` | Both store the transcript text as segments |
| `~/.scribe/uploads/`, `~/.scribe/capture/` | Recordings — transcribing or playing one reveals what was said |
| `/api/jobs/{id}/text`, `/transcript`, `/export` | Return transcript text |
| A script over transcript text | Still reads it, even if it prints only counts |
| `test/` in this repo | Exported transcripts (gitignored) |

**Test with synthetic data**: `say -o file.aiff "..."` for audio, invented
sentences for the cleaner — the unit tests already work this way. Reading a
job's status, or confirming a file exists without opening it, is fine. When
unsure, ask.

The `curl` examples against `/text` and `/export` elsewhere in these docs are
for the owner of the installation, and don't override this.

**Why this exists.** While verifying a fix to the cleaner, an assistant ran
scripts over every transcript in an installation without asking — having
earlier printed excerpts from several into its conversation, which sends them
off the machine. The owner hadn't agreed to either. This app captures things
people say out loud and don't intend to share; a check that seems harmless to
the person running it isn't the reader's call to make.

## Running it while you work

```bash
./start.sh                                  # everyday launcher
./.venv/bin/python -m pytest tests/ -q      # 25 tests, ~0.02s
```

Python changes need a restart (**Quit** in the UI, then `./start.sh`).
`index.html` is read from disk per request, so front-end changes are a browser
reload — no restart.

For iterating without a browser or a model, drive `JobStore` directly and
register a stub in `backends._REGISTRY` returning canned `Segment`s. That's how
the whole pipeline was exercised before a real model was ever run.

---

## Adding a transcription backend

The most likely real change. The `Backend` Protocol (`backends.py:37`) is the
only contract: a wav in, timestamped `Segment`s out.

1. **Write the class** in `scribe/backends.py`. It needs a `name` attribute and
   `transcribe(self, wav, language=None, prompt=None)`. Raise `BackendError`
   from `__init__` if the dependency is missing — check it there, not at import
   time, so one absent library doesn't break the whole app.
2. **Register it** in `_REGISTRY` (`backends.py:~210`).
3. **Add a preset** in `scribe/presets.toml`.

The part that surprises people: `build_backend` passes **every preset key except
`backend`, `label` and `language`** to your constructor as a keyword argument.
So adding `threads = 8` to a preset makes `threads` a constructor parameter —
if your `__init__` doesn't accept it, you get a `TypeError` at job time, not at
startup.

Return `[]` rather than raising if a chunk yields nothing; the worker treats an
empty chunk as legitimate silence. If your engine has no prompt conditioning,
accept `prompt` and ignore it — `ParakeetMlxBackend.transcribe` does exactly
this, with a comment saying so.

## Adding an export format

Formats live in one function and one array:

1. **`jobs.export()`** (`jobs.py:366`) — add a branch. Existing branches:
   `txt`, `md`, `raw`, `srt`, `vtt`. Raise `ValueError` for unknown formats;
   `app.export_job` turns that into a 400.
2. **`FORMATS`** in `static/index.html:392` — a `[value, label]` pair. Without
   this the format works via URL but never appears in the UI.
3. Optionally **`_write_markdown`** (`jobs.py:396`) if it should be written
   automatically on completion rather than only offered as a download.

Note `md` and `raw` are the *same paragraphs* with cleaning on or off — see
`clean.render(paras, cfg, clean=True|False)`. A new prose format almost
certainly wants `clean.to_paragraphs` rather than raw `job.segments`.

## Adding a config setting

**Two places, always:**

1. `scribe/config.toml` — the documented default a user edits.
2. `settings.py:_DEFAULTS` — the fallback.

Both, because `load_config()` deep-merges the file over `_DEFAULTS`; a key that
exists only in the TOML will `KeyError` for anyone whose `config.toml` predates
your change, and `load_config()` silently falls back to `_DEFAULTS` wholesale if
the file fails to parse.

Then read it where it's used. Chunking values are read once at the top of
`JobStore._run` and passed as arguments — `audio.py` never imports settings,
which is what keeps the planner unit-testable without config.

**Which file?** `presets.toml` is *which model*. `config.toml` is *everything
else*. They're split because they change on different timescales.

---

## Conventions

**Errors.** Module-specific exceptions: `AudioError`, `BackendError`,
`Cancelled`. The worker loop catches `Exception` broadly and marks the job
`failed` with the message — one bad job must never kill the thread. Route
handlers raise `HTTPException`.

**Logging.** There is none, deliberately. uvicorn's access log plus
`traceback.print_exc()` in the worker. Don't add a logging framework for one
module's benefit.

**Naming.** Private helpers take a leading underscore (`_drop_repeats`,
`_write_markdown`, `_emit`). Tests import them directly — that's intended.

**Tests** go in `tests/test_scribe.py`. They are pure-logic only: no ffmpeg, no
model, no HTTP. If a change needs a subprocess or a network call to test, that's
a signal the logic should be extracted into a pure function first — `plan_chunks`
and `clean_text` are both shaped that way for exactly this reason.

**Comments** explain *why*, not what. The existing ones carry real reasoning
(the hallucination window, the `min_chars` floor); match that or leave it alone.

---

## Gotchas

Ordered roughly by how likely you are to hit them.

**1. `test/` is not `tests/`.** `tests/` is the suite. `test/` holds the
owner's exported transcripts — user data, not code. Don't tidy it away.

**2. The checkpoint is only reachable through `JobStore.retry()`.** `retry()`
re-queues the *same job id*, so it lands on the same directory and finds
`checkpoint.json`. A fresh `submit()` mints a new uuid, gets a new directory,
and will never see it. If you add another path that re-runs a job, reuse the id
or checkpointing silently becomes dead code again — which is what it was before
`retry` existed.

**3. `delete_job` ordering is load-bearing.** `app.py:92` calls
`store.remove()` *before* `shutil.rmtree`. `remove()` sets the `cancelled` flag
that the worker checks between chunks. Swap those two lines and you're deleting
a directory the worker is still writing into.

**4. Chunk timestamps stay on the original timeline.** `plan_chunks` skips
silence but does **not** renumber time — chunk 2 may start at 74.8s even though
only 30s of audio precede it. This is deliberate: the transcript has to line up
with the original recording for playback. Never "optimise" by compressing the
timeline.

**5. `_drop_repeats(min_chars=25)` — the floor is the feature.** Without it the
filter deletes ordinary speech, because thinking out loud is full of repeated
short beats ("Right." "Okay." "Yeah."). A genuine decoder loop is always a long
phrase. There's a test pinning this; if it fails, don't relax the test.

**6. `slice_wav` puts `-ss` before `-i` on purpose** (`audio.py:231`). After
`-i` is output-side seeking, which decodes and discards everything before the
mark — making slicing quadratic on a long file. It looks like a style detail.
It isn't.

**7. `mlx_whisper.transcribe` has no named `language` parameter.** It arrives
through `**decode_options`. Inspecting the signature suggests we're passing an
unsupported argument; we aren't.

**8. `build_backend` caches instances** keyed on `json.dumps(preset,
sort_keys=True)` (`backends.py:217`). This is why a large model loads once
rather than per chunk. Editing a preset produces a different key and therefore a
new instance — expected, but it means an old instance can linger in `_CACHE` for
the process lifetime.

**9. `start.sh` exports Homebrew onto `PATH`.** Finder-launched apps get a bare
`PATH` without `/opt/homebrew/bin`. Python inherits it, so removing that line
hides ffmpeg from *every transcription call*, not just the startup check.

**10. Keep the project out of `~/Downloads`, `~/Desktop` and `~/Documents`.**
macOS TCC blocks an unsigned `Scribe.app` from executing anything inside those,
with `Operation not permitted` before any code runs. A folder directly in `$HOME`
is fine. (This is why the project lives at `~/scribe`.)

**11. `[hidden] { display: none !important; }` must stay** and must come after
`.record` in `index.html`. `.record` sets `display: inline-flex`, and a class
selector outranks the browser's built-in `[hidden]` rule — without the override
the Pause button is visible before recording starts.

**12. Chunk uploads are chained, and `onstop` awaits them.** Fire-and-forget
lets `/finish` reach the server before the last blob, which 404s on a recording
that genuinely exists, or silently truncates the tail.

**13. `refresh()` skips re-rendering when the payload is unchanged**
(`index.html:~511`). The poll runs every second and rewrites the list via
`innerHTML`; without the signature check it wipes any text you had selected in
an open transcript.

**14. `output_folder()` runs inside `lifespan`.** An unwritable output folder is
a startup failure, not a first-job failure. That's intentional — fail loudly
before recording an hour of audio you can't save.

**15. Only `MlxWhisperBackend` has been run against a real model.**
`WhisperCppBackend` and `ParakeetMlxBackend` are written but unproven. Treat
them as drafts.

**16. The menu bar app is a client, not a rewrite.** `ScribeApp/` records
natively and then POSTs to the *same* `/api/upload` the browser uses, so the
Python pipeline has no idea which client it is serving. Adding a server feature
means the app gets it too; changing a response shape means checking
`API.swift`'s `JobInfo`, which decodes `/api/jobs` and will silently return nil
on a missing field.

**17. Notifications are unavailable to unsigned builds.**
`UNUserNotificationCenter.requestAuthorization` fails with `UNErrorDomain
Code=1` for an ad-hoc-signed app, and LaunchServices re-registration does not
help — it needs a real Developer ID. `Notifier` therefore falls back to
`osascript display notification`, which can't carry a click action, which is
why `AppDelegate` opens the transcript directly on that path. Fixing this
properly costs $99/yr, nothing less.

**18. Each rebuild re-prompts for the microphone.** `build.sh` ad-hoc signs, so
the signature changes every build and macOS treats it as a new app. Normal use
(binary unchanged) prompts once.

**19. Clearing `backends._CACHE` alone frees nothing.** `mlx_whisper` keeps the
loaded model on `transcribe.ModelHolder.model`, its own class attribute, so
`release_idle` must clear that too and then call `mx.clear_cache()`. Measured:
1.51 GB active + 0.84 GB cache returned to zero. Process RSS is the wrong way
to check this — MLX allocates through Metal and barely shows there.

**20. Silence detection depends on the audio being levelled first.**
`noise_db` is an absolute threshold, so it only means something if inputs
arrive at a consistent level — and they don't: browser capture applies AGC,
`AVAudioRecorder` doesn't, a 15-30 dB difference. `to_wav` normalises for this
reason. If you disable `[chunking].normalize`, quiet recordings will be
classified as silence end to end. Use a *constant* gain if you change it;
dynamic normalisation raises the noise floor during pauses and destroys the
paragraph breaks.

**21. A job with no detected speech fails rather than producing an empty file.**
`_run` retries 15 dB lower first, then raises `AudioError`. Don't "fix" that by
letting it write an empty transcript — silent data loss is what it was added to
stop.

**22. Recording lives in the page.** Closing the tab mid-recording loses the
session — uploaded chunks stay on disk but `/finish` never fires, so no job is
created and the audio is orphaned in `~/.scribe/uploads`. A *transcription* in
progress is unaffected; it's on the server.
