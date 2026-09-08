# Features added since the original drop

## About this document

**There is no version control in this project** — no `.git`, no commits, tags or
branches. So the baseline here is the archive the project was originally
delivered as, still present at
`~/Downloads/local macwhisper .zip`: nine flat files,
40,831 bytes. Every diff below is reproducible read-only:

```bash
Z="$HOME/Downloads/local macwhisper .zip"   # wherever you kept the original
diff <(unzip -p "$Z" audio.py) scribe/audio.py
```

Because there are no commit messages, PR descriptions or issue references, the
**Why** in each section is taken strictly from code, docstrings, comments and
config. Where a reason is not recoverable from those, the section says so rather
than guessing.

### Change summary vs baseline

| Baseline file | Change |
|---|---|
| `jobs.py` | +218 / −27 |
| `index.html` | +279 / −17 |
| `app.py` | +106 / −26 |
| `audio.py` | +100 / −24 |
| `backends.py` | +20 / −4 |
| `presets.toml`, `requirements.txt`, `run.sh` | unchanged |

Net-new: `scribe/clean.py`, `scribe/settings.py`, `scribe/config.toml`,
`scribe/__init__.py`, `start.sh`, `tests/test_scribe.py`, `Scribe.app/`,
`Start Scribe.command`, `.gitignore`, `docs/`.

New endpoints: `GET /api/config`, `GET /api/jobs/{id}/text`,
`POST /api/jobs/{id}/retry`, `POST /api/quit`.

---

## 1. Scribe as a native menu bar app

**What it does.** Recording no longer needs a browser. `Scribe.app` sits in the
menu bar, captures audio natively, and opens the finished transcript by itself.
It also owns the Python engine as a child process, so there is no server to
start or stop — launching the app is the whole ritual. The baseline had no app
at all; capture required opening a browser tab and clicking Record.

**Why.** The friction was the number of steps between having a thought and
capturing it. `MENUBAR_APP.md` records the two shaping constraints: wrapping the
web UI in a `WKWebView` was rejected because `getUserMedia` inside a webview
needs the bundle signed and entitled before macOS grants microphone access,
and a full native rewrite was rejected because it would mean re-implementing
the chunk planner, cleaner, exports and persistence, and abandoning their tests.

**How it's implemented**

- `ScribeApp/` — **new**, ~900 lines of Swift across 7 files, built with
  `swift build` from the Command Line Tools. No Xcode, no developer account.
  - `AppDelegate.swift` (406) — status item, menu, and a forward-only state
    machine: `starting → idle → recording → uploading → transcribing → idle`,
    with `serverDown` terminal. All AppKit mutation funnels through `render()`.
  - `Server.swift` (123) — spawns `start.sh` with `SCRIBE_NO_OPEN=1`, adopts an
    already-running engine rather than starting a second, and shuts down via
    `/api/quit` before terminating the child.
  - `Recorder.swift` (76) — `AVAudioRecorder` to 16 kHz mono WAV, Whisper's
    native format, streamed to disk at ~115 MB/hour.
  - `API.swift` (99) — `URLSession` client; the multipart body is assembled in
    a temp file and sent with `uploadTask(fromFile:)`.
  - `Notifier.swift` (108), `Log.swift` (37), `main.swift` (10).
- `Scribe.app/` — **replaced**. Was a shell stub that opened a browser tab; is
  now the built bundle, ad-hoc signed, `LSUIElement` so there is no Dock icon.

**Two settings that exist because of macOS constraints rather than taste.**
*Open Transcript When Ready* compensates for notifications not being clickable
on an unsigned build (see Caveats). *Open Transcripts In* overrides the app used
for transcripts without touching the system-wide `.md` handler — worth having
because the global default is usually a code editor, which is the wrong tool for
reading back a rant but the right one for a repo. Both route through the single
`AppDelegate.openTranscript`, which is also what the notification click and the
Recent menu use, so the three paths cannot drift apart.

**Key design decision — it is a client, not a rewrite.** Native capture ends
with a POST to the *same* `/api/upload` the browser uses. The Python pipeline
was not modified for it at all, so both clients stay in step and the web UI
remains as the library view.

**How to use / test it.** Double-click `Scribe.app`; the menu has Start/Stop
Recording, Model, Recent, Open Library, Open Transcript When Ready, Open
Transcripts In, Launch at Login, and Quit.

**No automated tests.** Swift has no test target, and the recording path needs
a microphone. Verified by hand: app launch spawns the engine, upload → job →
transcript, quit stops both with a graceful uvicorn shutdown and no orphan.

**Caveats**

- **Notifications are unavailable to unsigned builds** —
  `UNUserNotificationCenter` returns `UNErrorDomain Code=1` regardless of
  LaunchServices registration. The AppleScript fallback can't carry a click, so
  *Open Transcript When Ready* opens the file directly instead. A real fix costs
  $99/yr.
- Each rebuild changes the ad-hoc signature, so macOS re-prompts for the
  microphone during development.
- Sleep mid-recording and audio device changes (AirPods connecting) are
  unhandled and unverified.
- One worker thread server-side, so two recordings submitted together queue.

---

## 2. Speech-only chunk planning and paragraph structure

**What it does.** Long silences are now *skipped* rather than sliced into
chunks and sent to the model. A recording with a 45-second thinking pause no
longer produces chunks made entirely of room tone, and less audio reaches the
model overall. As a by-product, every pause longer than `paragraph_gap` becomes
a paragraph break, so transcripts arrive with structure instead of as one
undifferentiated block.

**Why.** Fully documented in the `plan_chunks` docstring (`scribe/audio.py:186`).
The baseline cut the timeline at pause *midpoints*, which is correct for
half-second gaps between sentences and wrong for long ones — a forty-second
pause got sliced into chunks that were "*entirely room tone*, and room tone is
precisely the input Whisper hallucinates on: it was trained on subtitle tracks,
so silence decodes to 'Thanks for watching!'". Skipping silence removes the
failure mode at source. The docstring also notes the cost saving as secondary,
not the motivation.

**How it's implemented**

- `scribe/audio.py` — the only module changed for this feature (+100 / −24).
  - `speech_regions()` (`:131`) — new. Inverts silence spans into speech spans,
    pads each edge by `pad` (default 0.25s) so word onsets survive the cut, and
    drops slivers under `min_speech` (0.15s).
  - `plan_chunks()` (`:179`) — rewritten. Walks the speech regions accumulating
    them into a chunk; a gap ≥ `paragraph_gap` flushes the chunk **and** flags
    the next one with `para_break`; a gap below it is treated as speech rhythm
    and kept inline.
  - `_emit()` (`:160`) — new. Splits an over-long span into *equal* parts rather
    than repeatedly shaving off `max_len`. The docstring gives the reason:
    shaving "avoids leaving a two-second orphan at the end, which would cost a
    whole model invocation for almost no audio."
  - `Chunk` (`:120`) gains a `para_break: bool` field (`:124`), commented "a thinking pause
    preceded this chunk."
- `scribe/jobs.py:229` is the only caller; it passes the `[chunking]` config
  values through.

**Key design decision — timestamps are never renumbered.** `speech_regions`'
docstring is explicit: "Timestamps stay on the original timeline throughout —
nothing is compressed or renumbered — so every offset the model reports still
lines up with the recording you can play back." Chunk 2 may legitimately start
at 74.8s with only 30s of audio before it. Compressing the timeline would be an
obvious optimisation and would break playback alignment.

Note that `plan_chunks` replaced the baseline's `min_len` parameter with
`paragraph_gap` and `pad` — a signature change, not an addition.

**How to use / test it.** No user-facing switch; it is the pipeline. Tuned via
`[chunking]` in `scribe/config.toml` (`paragraph_gap`, `noise_db`,
`min_silence`, `max_chunk`, `pad`).

Well covered — 11 of the 25 tests, in `tests/test_scribe.py:33-99`. The
regression test for the original bug is
`test_long_pause_is_skipped_not_transcribed` (`:49`), which asserts no chunk
overlaps a 45-second pause. `test_timestamps_stay_on_the_original_timeline`
(`tests/test_scribe.py:88`) pins the design decision above.

**Caveats**

- Silence detection is only as good as `noise_db` (−35 dB default). In a room
  with a high noise floor `silencedetect` finds no pauses, and the planner
  degrades to clock-cut chunks at `max_chunk` with no paragraph breaks at all.
  There is no auto-calibration and no warning when this happens.
- `pad` can make adjacent padded regions overlap, so a word near a short gap can
  in principle be transcribed twice. In practice regions closer than
  `paragraph_gap` (2.0s) merge into one chunk, and `2 × pad` is 0.5s, so the
  overlap case is unreachable with default settings — but it is not guarded
  against if those values are changed.
- `plan_chunks` returns `[]` for silence-only input (pinned by
  `test_silence_only_input_plans_nothing`). `jobs._run` handles this without
  error, but still writes empty `.md` and `.raw.md` files.

---

## 3. Cleaned and verbatim output, written to disk automatically

**What it does.** Every finished job now writes **two** markdown files to a
configured folder without any manual export step: a cleaned version with filler
words stripped, and a verbatim `.raw.md` beside it. The baseline offered
`txt`/`srt`/`vtt`/`md` as download links only — nothing was ever written to
disk, and there was no cleaned/verbatim distinction.

**Why.** Two reasons, both stated in `_write_markdown`'s docstring
(`scribe/jobs.py:396`): "A transcript that needs a manual export step is a
transcript you stop collecting", and "Written as a pair so the cleaning can be
aggressive: the raw file means nothing is ever actually lost."

The tiering of filler words is explained in the `clean.py` module docstring:
filler words "are not one category. 'Um' carries no meaning and can always go.
'I mean' is usually filler but sometimes a genuine self-correction. 'Actually'
often changes the sentence." They are separated by how much is lost when the
guess is wrong, and only the safe tier runs by default.

**How it's implemented**

- `scribe/clean.py` — **new**, 96 lines, no I/O.
  - `clean_text()` (`:56`) applies three configured tiers in order: `always`,
    `phrases`, `aggressive`, then optional stutter collapse.
  - `_strip_tokens()` (`:29`) handles the awkward case first — a filler between
    two commas takes *both* commas with it, or "I was, uh, thinking" would
    become "I was, thinking".
  - `_tidy()` (`:43`) repairs stranded punctuation and re-capitalises sentence
    starts, since removing a leading "Um," lowercases the sentence.
  - `to_paragraphs()` (`:68`) joins segments into paragraphs at the break
    indices produced by feature 1.
  - `render()` (`:93`) is the single switch between cleaned and verbatim — both
    versions are the *same paragraphs* with cleaning on or off.
- `scribe/jobs.py`
  - `export()` (`:366`) gains a `raw` format and now routes `txt` and `md`
    through `clean.to_paragraphs`. **Behaviour change:** baseline `txt` was
    `"\n".join(s.text for s in segs)` — raw segments, one per line. It is now
    cleaned, paragraphed prose.
  - `_write_markdown()` (`:396`) writes the pair as
    `YYYY-MM-DD-HHMM-<slug>.md` / `.raw.md`, called at the end of `_run`.
  - `_front_matter()` (`:352`) and `_slug()` (`:347`) are new helpers.
  - `Job.written: list[str]` carries the paths back to the UI.
- `scribe/config.toml` — new `[output]` (`folder`, `keep_audio`) and
  `[cleaning]` (`always`, `phrases`, `aggressive`, `fix_stutters`) sections.

**Design decisions worth knowing.** The `aggressive` tier ships **empty** — the
config comments list `like`, `basically`, `actually`, `literally`, `right` as
candidates but leaves them commented out, because they "carry real meaning often
enough to be worth keeping." Cleaning is regex-based, not model-based; there is
no LLM pass anywhere in this feature.

**How to use / test it.** Automatic on job completion; output location is
`[output].folder` in `scribe/config.toml` (default `~/Documents/scribe`). The
same text is available at `GET /api/jobs/{id}/export?format=md|raw|txt` and in
the UI's export links.

Covered by 5 tests (`tests/test_scribe.py:105-135`): filler stripping is
parametrised, plus `test_clean_leaves_meaningful_words_alone`,
`test_clean_is_idempotent`, and two paragraph-assembly tests.
`_write_markdown` itself has **no test** — nothing exercises the filesystem
write, the slug, or the front matter.

**Caveats**

- `_write_markdown` catches only `OSError`. It records the failure in
  `job.error` and returns `[]` — but this runs *after* `job.status` is set by
  the worker loop, so a job whose files failed to write still reports `done`
  with an error message attached. Non-obvious, and untested.
- Filenames are derived from `_slug(job.name)` plus a minute-resolution
  timestamp. Two jobs finishing in the same minute with the same name overwrite
  each other silently — `path.write_text` does not check for existence.
- `_STUTTER` (`clean.py:19`) collapses *any* immediate word repetition, so
  legitimate doubles ("had had", "that that") are also collapsed. Acceptable
  because the raw file is preserved, but it is not a targeted fix.
- `keep_audio` in `[output]` is **dead config**. It appears only in
  `config.toml:13` and `settings.py:19` and is never read. The behaviour it
  describes does happen — the decoded PCM is deleted (`jobs.py:291`) and the
  source recording is kept — but unconditionally, so setting it to `false`
  changes nothing.

---

## 4. Job persistence and a resume that actually works

**What it does.** The job list now survives a server restart, and a failed or
interrupted job can be resumed from its last completed chunk with a **Resume**
link. In the baseline, jobs existed only in memory: restarting the server
erased the list entirely, and there was no way to re-run a job at all.

**Why.** The baseline already wrote `checkpoint.json` after every chunk, and its
docstring called it "the point of this module" — but nothing could ever read it
back. `submit()` mints a fresh `uuid4().hex[:12]` per job, so a resubmission got
a new directory and never saw the old checkpoint, and no other code path
re-queued a job. The current docstring states the fix explicitly: the checkpoint
"is only reachable because `retry` re-queues the *same* job id, and so lands on
the same directory — a fresh submission would get a fresh uuid and a fresh empty
checkpoint, which is the same as not having one."

So this feature is best read as *making an existing, non-functional feature
reachable* rather than as new machinery.

**How it's implemented**

- `scribe/jobs.py` (+218 / −27 overall; this is the bulk of it)
  - `Job.save()` (`:63`) — serialises the job to
    `~/.scribe/jobs/<id>/job.json`, including `segments` and `breaks`. Wrapped
    in `try/except OSError` with the comment "never fail a transcription over
    its own bookkeeping."
  - `Job.load()` (`:80`) — classmethod, tolerant: returns `None` on `OSError`,
    `JSONDecodeError` or `KeyError` rather than raising.
  - `JobStore._restore()` (`:141`) — globs `WORK_DIR/*/job.json` at construction
    and repopulates `_jobs`.
  - `JobStore.retry()` (`:160`) — re-queues the **same job id**, clearing
    `status`, `error`, `cancelled`, `segments` and `breaks` but keeping the
    directory. Refuses if the job is already `running`.
  - `Job.cancelled` and the `Cancelled` exception (`:30`) — checked at the top
    of each chunk iteration so a deleted job stops promptly.
- `scribe/app.py` — new `POST /api/jobs/{job_id}/retry` (`:104`), returning 404
  if the job is missing *or already running*.
- `scribe/static/index.html` — a **Resume** link rendered for `failed` jobs.

**Key design decision — interrupted jobs are downgraded on load.** `Job.load`
rewrites any `running` or `queued` status to `failed` with the message
"Interrupted — press Resume to pick up where it stopped." The comment gives the
reason: otherwise you get "a progress bar that never moves." A restored job is
never silently believed to be still running.

Two persistence files with different jobs, easy to confuse: `job.json` is the
job record (metadata, final segments), `checkpoint.json` is per-chunk
transcription output used only to skip work on resume.

**How to use / test it.** Resume appears on failed jobs in the UI; directly,
`curl -X POST localhost:8765/api/jobs/<id>/retry`. Persistence needs no action —
restart the server and the list is still there.

**No tests cover any of this.** `tests/test_scribe.py` is pure-logic only;
`Job.save`/`load`, `_restore` and `retry` all touch the filesystem or the
worker thread and are untested. This is the largest untested surface added.

**Caveats**

- `retry()` clears `job.segments` before re-queueing, so if `_run` fails early
  (say the audio source has been deleted) the job loses the transcript it
  previously had. The checkpoint still holds the chunk data, but `job.json` is
  overwritten with the empty result when the worker next saves.
- `_restore()` never prunes. Every job directory ever created is reloaded on
  every start, and nothing deletes old ones except an explicit Remove in the UI.
  On a long-lived install the list and the startup glob grow without bound.
- Deleting a job depends on ordering that is easy to break: `app.delete_job`
  (`app.py:92`) calls `store.remove()` — which sets `cancelled` — *before*
  `shutil.rmtree`. The worker only checks `cancelled` between chunks, so a
  delete lands mid-chunk at worst; the in-flight chunk still writes into a
  directory that is about to disappear.
- The source audio path is stored as an absolute string in `job.json`. Moving
  `~/.scribe/uploads`, or the project, breaks Resume for existing jobs with no
  migration path.

---

## 5. In-browser transcript preview

**What it does.** Finished jobs get a **Read** link that expands the transcript
inline, with a Cleaned/Raw toggle, a word count and a Copy button. Previously
the only way to see a transcript was to download an export file or open the
markdown on disk.

**Why.** Stated in the `job_text` docstring (`scribe/app.py:123`): it exists
"for reading in the browser", and is "Separate from /export because that returns
a file with front matter and a download header. This is the same text with the
packaging removed." Why a preview was wanted at all is **not discernible from
the code** — no comment or issue reference explains the motivation.

**How it's implemented**

- `scribe/app.py` — new `GET /api/jobs/{job_id}/text?format=md|raw` (`:122`).
  Returns `{"paragraphs": [...], "words": n}`. It reuses `clean.to_paragraphs`
  and `clean.clean_text` rather than duplicating logic; `format=md` cleans,
  anything else returns verbatim. Note the format check is `if format == "md"`,
  so any unrecognised value silently yields raw text rather than a 400 — unlike
  `/export`, which raises.
- `scribe/static/index.html` — the bulk of the +279 lines.
  - Two module-level `Map`s (`:397-398`): `opened` (jobId → format) and
    `textCache` (`jobId:fmt` → payload). Both live outside `render()`
    deliberately, commented "so the once-a-second poll can redraw the list
    without collapsing what you're reading."
  - `loadText()` (`:400`) fetches and memoises per job+format.
  - `previewHtml()` (`:408`) renders the panel from cache, showing "Loading…"
    until the fetch resolves.
  - Handlers for `data-read`, `data-fmt` and `data-copy`; Copy uses
    `navigator.clipboard.writeText`.
  - `.preview` CSS constrains the body to `max-height: 460px` and paragraphs to
    `62ch`.

**Key design decision — the poll no longer re-renders blindly.** `refresh()`
(`:511`) now computes a signature of the jobs payload plus the `opened` map and
returns early when unchanged. The comment states the reason: rewriting identical
markup once a second "would wipe any text you had selected in an open
transcript." Callers that change local state pass `refresh(true)` to force a
redraw. Without this the preview would be unusable for its actual purpose.

**How to use / test it.** Click **Read** on a finished job in the UI; click
again to Hide. Directly:
`curl 'localhost:8765/api/jobs/<id>/text?format=raw'`.

**No automated tests.** The endpoint and all the front-end state handling are
untested — `tests/test_scribe.py` covers no HTTP and no JavaScript. The
underlying `clean.to_paragraphs` and `clean_text` are tested, so the text
content is indirectly covered; the delivery path is not.

**Caveats**

- `textCache` is never invalidated. If a job is re-run via Resume while its
  preview has been opened, the panel keeps serving the previous transcript until
  the page is reloaded.
- Unknown `format` values return raw silently (see above) — an inconsistency
  with `/export`'s 400.
- The whole transcript is sent and rendered in one go, with no pagination. Fine
  at the observed scale (a few hundred words); a multi-hour transcript would
  build a very large DOM string via `innerHTML` on every forced redraw.
- `navigator.clipboard` requires a secure context. It works on `localhost` but
  would fail silently over a LAN address, where the app already does not work
  for other reasons.

---

## 6. Reliable browser recording

**What it does.** Recording now reports what it is doing — waiting for
microphone permission, bytes uploaded, or the specific failure — instead of
appearing to do nothing when it fails. It also has a Pause button, correctly
names the uploaded file for the browser's actual container format, and no
longer races its own uploads when you press Stop.

**Why.** The comments name each problem directly. On the status line
(`index.html:~380`): recording "fails in a handful of boring ways — a dismissed
permission prompt, a browser without mic access on this origin, macOS privacy
settings — and every one of them used to look identical: nothing happens."
Specifically on the permission case: "A dismissed permission prompt never
settles this promise, so say we're waiting. Silence here is what 'nothing
happened' actually looked like."

On the upload race: the final blob "arrives just before this fires, and its
upload is still in flight. Without this wait, `finish` can beat it to the server
and 404 on a recording that does exist — or miss the last few seconds of one."

On the container: the format is "the browser's choice — Chrome gives WebM,
Safari MP4 — and the server names the file after it, because appending MP4
fragments the way you can append WebM clusters does not produce a decodable
file."

**How it's implemented**

- `scribe/static/index.html`
  - `status(msg, bad)` writes to a `#status` element; the old code had a single
    `alert()` for permission failure and no feedback anywhere else.
  - Uploads are **chained, not parallel**: `uploads = uploads.then(async () =>
    ...)` accumulates a promise chain, and `recorder.onstop` does `await
    uploads` before calling `/finish`. The baseline was fire-and-forget
    (`recorder.ondataavailable = async (e) => { await fetch(...) }`), where the
    `await` only suspended the handler, not `onstop`.
  - Serialising also fixes ordering — every blob appends to one file, so
    concurrent POSTs could interleave.
  - `recorder.mimeType` is sent as the `Content-Type` of each chunk.
  - `recorder.onerror` surfaces recorder-level failures.
  - Pause/Resume via `recorder.pause()`/`resume()`, with `banked` accumulating
    elapsed time across pauses so the clock stays correct.
- `scribe/app.py`
  - `_rec_path()` (`:186`) — new helper. With an extension it builds the path;
    without one it globs `rec-<id>.*` to find whatever was written.
  - `record_chunk` (`:199`) maps the request's `Content-Type` subtype through
    `_CONTAINERS` (`:33`) to an extension, defaulting to `.webm`. The baseline
    hardcoded `.webm` for every browser.

**Design decision worth knowing — it stays in a real browser.** The README
records the rejected alternative: an embedded `WKWebView` would need the bundle
signed and entitled before macOS grants microphone access, whereas the
permission already granted to `localhost` keeps working. Feature 8's app-mode
window is the cosmetic compromise.

**How to use / test it.** The Record button in the UI; watch the status line
under the transport. Pause appears only while recording.

**No automated tests.** Nothing exercises `MediaRecorder`, the chunk endpoint,
or `_rec_path`. This is browser-side and filesystem code, both outside the
pure-logic suite.

**Caveats**

- **Safari is untested.** The `Content-Type` plumbing exists specifically to
  handle its MP4 output, but per the README no one has run it, and appending
  MP4 fragments is not equivalent to appending WebM clusters. If it fails, the
  failure will be at ffmpeg decode, not at upload.
- Closing the tab mid-recording loses the session: uploaded chunks remain in
  `~/.scribe/uploads` but `/finish` never fires, so no job is created and the
  file is orphaned. Nothing cleans these up.
- `record_chunk` appends to whatever `rec-<id>.<ext>` already exists. Recording
  ids are `Math.random().toString(36).slice(2, 12)` — collision is unlikely but
  would silently concatenate two recordings into one file.
- An upload failure sets the status line red but does **not** stop the
  recording; it carries on, and the resulting file will have a gap.
- `_CONTAINERS` covers webm/ogg/mp4/mpeg/wav. Anything else falls back to
  `.webm` regardless of actual content.

---

## 7. Vocabulary bias and verbatim prompting

**What it does.** A configurable prompt is now passed to the transcription
backend on every chunk. It carries two things: a deliberately disfluent sample
sentence that pushes Whisper toward verbatim output, and a user-maintained
vocabulary list that biases spelling of names the model would otherwise mangle.
The baseline passed no prompt at all.

**Why.** Both halves are documented. From `settings.initial_prompt`'s docstring
(`scribe/settings.py:69`): "Whisper imitates the style of whatever it's primed
with, so the sample sentence pulls it toward verbatim instead of tidying as it
decodes, and the word list biases spelling of names it would otherwise mangle."

`config.toml:42` adds the consequence for feature 2: seeded with a disfluent
sample, Whisper "transcribes far closer to verbatim instead of tidying as it
goes — which is what makes a genuinely raw version possible at all." In other
words the `.raw.md` file is only meaningfully different from `.md` because of
this feature. On the vocabulary list (`config.toml:47`): "for ranting about your
own work this is the single biggest accuracy win."

**How it's implemented**

- `scribe/settings.py` — `initial_prompt()` (`:69`) concatenates
  `[transcription].verbatim_prompt` and a space-joined `vocabulary` list,
  returning `None` if both are empty.
- `scribe/backends.py` (+20 / −4 — the whole of this file's change is this
  feature) — `prompt: str | None = None` added to the `Backend` Protocol
  (`:40`) and all three implementations:
  - `MlxWhisperBackend` (`:137`) passes it as `initial_prompt=`.
  - `WhisperCppBackend` (`:87`) appends `--prompt <text>` to the CLI args.
  - `ParakeetMlxBackend` (`:181`) **accepts and ignores it**, with a comment
    explaining Parakeet "is a CTC/TDT model with no prompt conditioning, so
    `prompt` is accepted and ignored rather than pretended at."
- `scribe/config.toml` — new `[transcription]` section.
- `scribe/jobs.py:248` builds the prompt once per job and passes it to every
  chunk.

**Design decision worth knowing.** The prompt is rebuilt per job, not per
chunk, and is identical for every chunk. That is consistent with
`condition_on_previous_text=False`: each chunk is primed the same way and none
inherits context from its predecessor.

**How to use / test it.** Edit `vocabulary` in `scribe/config.toml` (it ships
empty, with commented examples) and restart. `verbatim_prompt` is tunable in the
same section.

**No tests.** `initial_prompt` is pure and trivially testable but has no test;
neither does the threading through backends.

**Caveats**

- **Nothing bounds the prompt length.** `initial_prompt` concatenates the
  sample sentence and the entire vocabulary list with no size check, and no
  caller checks either. Whisper's decoder has a finite prompt window, so a
  long enough vocabulary list would begin crowding out the verbatim sample —
  and with it the raw/cleaned distinction. That window is a property of the
  model rather than of this repo, so the exact threshold is not verifiable
  from the code here; what *is* verifiable is that nothing checks or warns.
- `ParakeetMlxBackend` ignores the prompt entirely, so selecting the `fast`
  preset silently disables both vocabulary bias and verbatim output. The
  README notes Parakeet "takes no prompt" but the UI gives no indication.
- The effect is not verifiable from the code — whether a given
  `verbatim_prompt` actually produces a more verbatim transcript is empirical,
  and there is no measurement or regression test for it.

---

## 8. Configuration system

**What it does.** Behaviour that was previously hardcoded — output folder,
silence thresholds, chunk length, filler-word lists, the decoder prompt — is now
editable in `scribe/config.toml` without touching Python. The baseline had only
`presets.toml`, which selected a model and nothing else.

**Why.** The split between the two files is explained in `settings.py`'s module
docstring: "`presets.toml` is which model to run, `config.toml` is everything
about how the app behaves. They change for different reasons and at different
times — you settle on a model and stop touching presets, while the filler list
and vocabulary get edited as you learn how you actually talk."

**How it's implemented**

- `scribe/settings.py` — **new**, 80 lines.
  - `_DEFAULTS` (`:18`) — a nested dict holding every default.
  - `_merge()` (`:32`) — recursive dict merge, file over defaults.
  - `load_config()` (`:42`) — returns `_DEFAULTS` outright if `config.toml` is
    missing *or* fails to parse. Docstring: "Deliberately tolerant: a missing or
    half-written config should degrade to working defaults rather than stop a
    recording from being transcribed."
  - `load_presets()` (`:57`), `output_folder()` (`:62`), `initial_prompt()`
    (`:69`).
- `scribe/config.toml` — **new**, 69 lines and roughly half comments; sections
  `[output]`, `[chunking]`, `[transcription]`, `[cleaning]`.
- `scribe/app.py` — `load_presets` moved out to `settings`; new
  `GET /api/config` (`:62`) exposing the resolved output folder to the UI.

**Design decision worth knowing — `audio.py` does not import `settings`.**
Chunking values are read once in `JobStore._run` and passed to `plan_chunks` as
arguments. That is what keeps the planner unit-testable with no config on disk,
and it is why all 11 planner tests can call `plan_chunks` directly.

**How to use / test it.** Edit `scribe/config.toml` and restart the server
(config is read per job, but the process caches nothing else). `curl
localhost:8765/api/config` returns the resolved output folder.

**No tests** cover `settings.py` directly. `_merge` is pure and would be the
easiest thing in the codebase to test.

**Caveats**

- **Adding a key to `config.toml` alone is not enough** — it must also go in
  `_DEFAULTS`, or anyone with an older `config.toml` gets a `KeyError` at job
  time. The merge only fills in keys that exist in `_DEFAULTS`.
- `load_config()` swallowing a `TOMLDecodeError` means a typo in your config
  silently reverts *every* setting to defaults with no warning printed. Tolerant
  as designed, but it will look like your edits are being ignored.
- `load_config()` is called fresh on each job and each `/api/text` request; there
  is no caching and no file-watching. Cheap, but it means config can change
  mid-run between jobs.
- `keep_audio` is defined and never read (see feature 2).

---

## 9. Launchers, Mac packaging, and Quit from the UI

**What it does.** The app can be started by double-clicking `Scribe.app` or
`Start Scribe.command`, and stopped with a **Quit** button in the UI — no
terminal needed in either direction. `start.sh` bootstraps everything: it finds
a suitable Python, creates the venv, installs dependencies when
`requirements.txt` changes, and opens the UI in a chromeless app window. The
baseline shipped only `run.sh`, a single `exec uvicorn` line assuming an
already-active venv.

**Why.** Recoverable in part. `quit_server`'s docstring (`scribe/app.py:74`)
explains the intent — "Stop the server from the UI, so it doesn't need a
terminal to shut down" — and why it is safe: "each chunk is checkpointed as it
lands and the job record persists, so an interrupted job reappears marked Resume."

`start.sh`'s comments explain the environment problems it solves: Finder "hands
a double-clicked app a bare PATH — /usr/bin:/bin and little else — so Homebrew
is invisible and ffmpeg isn't found. Python inherits this too, so it matters for
every ffmpeg call during transcription, not just the check." And the `.command`
file exists because Terminal "already has permission to read this folder,
whereas an unsigned .app launched from Finder does not."

Why an app window rather than a tab is given in `start.sh`'s `open_ui`: it is
"deliberately still a real browser rather than an embedded WKWebView: the
microphone is the entire point of this app, and getUserMedia inside a webview
needs the bundle signed and entitled before macOS will grant it."

**How it's implemented**

- `start.sh` — **new**, 144 lines, ~28% comments. `pick_python()` prefers
  Homebrew 3.11/3.12/3.13 by explicit path over a bare `python3`; dependency
  installs are gated on a `shasum` of `requirements.txt` stored at
  `.venv/.requirements-sha`; an `lsof` check plus a `$VENV/.starting` lock
  directory prevent two concurrent starts; `open_ui()` uses Chromium's `--app=`
  flag with a fallback to `open`.
- `Scribe.app/` — **new** minimal bundle: `Contents/Info.plist`,
  `Contents/MacOS/Scribe` (a shell stub that locates the project and redirects
  output to `~/.scribe/scribe.log`), `Contents/Resources/Scribe.icns`.
- `Start Scribe.command` — **new**, 14 lines; runs `start.sh` in Terminal.
- `scribe/app.py` — `POST /api/quit` (`:73`) collects the names of
  running/queued jobs, then triggers shutdown from a daemon thread with
  `os.kill(os.getpid(), signal.SIGINT)` after a 0.4s delay so the HTTP response
  reaches the browser first.
- `scribe/static/index.html` — a **Quit** button that confirms before
  discarding an in-progress recording or interrupting a running job, then
  replaces the page with a "Stopped" message and clears the poll interval.

**Environment variables introduced** (all read in `start.sh`): `SCRIBE_PORT`
(default 8765), `SCRIBE_BROWSER_WINDOW` (`0` forces a plain tab),
`SCRIBE_NO_OPEN` (start without opening a browser).

**How to use / test it.** Double-click `Scribe.app`, or `./start.sh`. Quit is in
the header. `SCRIBE_PORT=9000 ./start.sh` moves the port.

**No tests.** Shell scripts and the bundle are untested; `/api/quit` is
untestable in-process by nature since it kills the process.

**Caveats**

- **`Scribe.app` is unsigned**, so it only works from a non-TCC-protected
  folder. The README documents this: from `~/Downloads`, `~/Desktop` or
  `~/Documents` it fails with "Operation not permitted" before any code runs.
  The bundle's fallback path is hardcoded to `$HOME/scribe`.
- The `.starting` lock is stale-tolerant by age (2 minutes) rather than by
  checking whether a process still lives, so an unlucky start within that window
  after a crash reports "another start is already in progress" and exits.
- `os.kill(SIGINT)` gives uvicorn a graceful shutdown, but the worker thread is
  a daemon — an in-flight chunk is abandoned wherever it is. That is safe only
  because of the checkpoint, and the coupling is not enforced anywhere.
- `/api/quit` takes no authentication. It is bound to `127.0.0.1` only, so this
  is consistent with the rest of the app, but any page in the browser could
  issue the request.

---

## 10. Infrastructure: a runnable package and a test suite

**What it does.** The project can now be imported and started, and has 25 unit
tests. **The baseline could not run at all** — this is not a refactor.

**Why it couldn't run.** Verifiable directly from the archive. **Line numbers
marked *(archive)* refer to the baseline zip, not the current tree:**

- *(archive)* `app.py:18` and `jobs.py:19` use relative imports (`from . import audio`), but
  the nine files were flat with **no `__init__.py`** — so nothing formed a
  package. Importing raises `ImportError: attempted relative import with no
  known parent package`.
- `run.sh` targets `uvicorn scribe.app:app`, expecting a `scribe/` package that
  did not exist.
- *(archive)* `app.py:42` serves `BASE / "static" / "index.html"`, but `index.html` sat at
  the top level with **no `static/` directory**.
- `run.sh` shipped mode `600` — not executable, so the documented `./run.sh`
  fails with permission denied.

The baseline README also claimed at *(archive)* `README.md:112` that "the chunk planner, timecode
formatting and repeat filter are unit-tested." **No test file existed in the
archive.** That claim was false as shipped.

**How it's implemented**

- `scribe/` package created: `__init__.py` (empty), with the modules and
  `presets.toml` moved inside and `index.html` moved to `scribe/static/`.
- `tests/test_scribe.py` — **new**, 169 lines, 25 tests. Structure worth
  knowing: it `sys.path.insert`s the project root (`tests/test_scribe.py:14`) rather than relying on
  an installed package, and imports private helpers directly (`_drop_repeats`,
  `_slug`, `_timecode`) — testing internals is intentional here.
- Coverage is deliberately narrow: `speech_regions`/`plan_chunks` (11 tests),
  `clean` (5), `_drop_repeats` (3), timecode/slug (2), plus parametrised filler
  cases. Everything tested is a pure function — no ffmpeg, no model, no HTTP.
- `.gitignore` added (though the project is still not a git repository).

**How to use / test it.** `./.venv/bin/python -m pytest tests/ -q` → 25 passed
in about 0.02s.

**Caveats**

- The suite covers the pure core and **nothing else**. Untested: the entire HTTP
  layer, all three backends, the browser, job persistence, the launchers, and
  every filesystem write. Roughly speaking the tested surface is `audio.py`'s
  planner plus `clean.py`.
- `pytest` is installed in `.venv` but is **not in `requirements.txt`**, so a
  fresh clone that runs `start.sh` gets a venv without it and the documented
  test command fails until it's installed manually.
- Tests import private underscore-prefixed functions, so renaming an internal
  helper breaks the suite. That is a deliberate trade, not an oversight, but it
  makes those names part of the contract.

---

## 11. Correctness fixes with real effect

Grouped because each is small, but these change behaviour rather than style.

**`_drop_repeats` no longer deletes ordinary speech** (`scribe/jobs.py:294`).
The baseline dropped any segment whose text matched one of the previous three,
with no length floor. The docstring now explains the problem: "'Yeah.' twice in
a conversation is two real utterances, and thinking out loud is full of repeated
short beats — 'Right.' 'Okay.' 'So.' A runaway decode loop is always a long
phrase, so only long repeats are suspicious." A `min_chars=25` floor was added.
Pinned by `test_short_repeats_survive` and `test_long_repeats_are_dropped`. The
function also now remaps paragraph-break indices as segments are dropped, and
returns a tuple rather than a list — a signature change.

**`slice_wav` seeks the input, not the output** (`scribe/audio.py:231`). `-ss`
moved *before* `-i`. The comment gives the effect: output-side seeking means
ffmpeg "decod[es] and discard[s] everything ahead of the mark. On a two-hour
rant the output-side form makes slicing quadratic — every chunk re-reads the
file from zero."

**`check_tools` checks ffprobe** (`scribe/audio.py:40`). The baseline verified
only ffmpeg, but `duration()` shells out to ffprobe — so a machine with one and
not the other passed startup and failed mid-job.

**Uploads no longer collide** (`scribe/app.py:169`). The baseline wrote
`UPLOADS / Path(filename).name`; two files named `audio.m4a` overwrote each
other, including one a queued job still pointed at. Now prefixed with
`uuid4().hex[:8]`.

**`delete_job` cancels before deleting** (`scribe/app.py:92`). `store.remove()`
sets the `cancelled` flag the worker checks between chunks, and only then is
`shutil.rmtree` called.

**`[hidden]` CSS override** (`scribe/static/index.html:153`). `.record` sets
`display: inline-flex`, and a class selector outranks the browser's built-in
`[hidden]` rule — so `<button class="record" hidden>` stayed visible. The
comment marks it as load-bearing.

**Startup validates the output folder** (`scribe/app.py`, `lifespan`).
`output_folder()` is called at startup, so an unwritable folder fails
immediately rather than after an hour of recording.

**Caveats.** These were found by observation rather than by tests — only the
`_drop_repeats` floor has regression coverage. The `slice_wav`, `check_tools`,
upload-collision and delete-ordering fixes are all untested, and the CSS
override is the kind of thing a stylesheet refactor would silently undo.

---

## 12. Idle model eviction

**What it does.** The engine now unloads the transcription model after 10 idle
minutes, freeing ~1.5 GB, and reloads it on the next job.

**Why.** Recorded in the `release_idle` docstring (`scribe/backends.py`): the
menu bar app keeps the server alive indefinitely, so "left alone, the first
transcription of the day pins the model in memory until you quit." This did not
matter when the server was started and stopped around each use.

**How it's implemented**

- `scribe/backends.py` — `release_idle(after_seconds)` and `_release_mlx()`;
  `build_backend` stamps `_LAST_USED`.
- `scribe/jobs.py` — `_loop` now uses `self._queue.get(timeout=60)`; the
  `queue.Empty` branch is the idle tick. No extra thread, and because the
  worker is inside `_run` during a job, eviction **cannot** fire mid-job.
- `scribe/config.toml` and `settings._DEFAULTS` —
  `[transcription].unload_after_minutes = 10`, `0` to disable.

**The non-obvious part.** Clearing `_CACHE` frees nothing on its own:
`mlx_whisper` keeps the loaded model on `transcribe.ModelHolder.model`, a class
attribute of its own. `_release_mlx` clears that too, then calls
`mx.clear_cache()` to return Metal buffers. Measured: 1.51 GB active + 0.84 GB
cache → 0.00 GB. Process RSS is the wrong instrument here — MLX allocates
through Metal and barely shows in RSS at all.

**How to use / test it.** Automatic. Three tests cover it
(`test_release_idle_*` in `tests/test_scribe.py`), and the live server logs
`scribe: unloaded idle model after N min`.

**Caveats.** The next job pays a few seconds to reload. The idle tick only runs
when the queue is empty, so the clock effectively starts when the worker goes
idle, not when the last job ended.

---

## 13. Input level normalisation

**What it does.** Audio is levelled during ingest, so a quiet recording is no
longer mistaken for silence. Before this, a whole recording could be discarded
and an empty transcript written with no error.

**Why.** Measured across real recordings: the browser's `getUserMedia` applies
automatic gain control, `AVAudioRecorder` does not, so the same voice arrived
**15–30 dB quieter** from the menu bar app (−37 to −56 dB mean) than from a tab
(−22 to −24 dB). Against a fixed −35 dB silence threshold that difference
decides everything. A real 13.7-second note measured −56 dB mean, produced
**0 chunks and 0 words**, and was written out as an empty `.md`.

**How it's implemented**

- `scribe/audio.py`
  - `measure_levels()` — mean and peak dBFS from ffmpeg's `volumedetect`.
  - `gain_db()` — pure; aims the *mean* at −20 dB but yields to peak headroom,
    so it never clips, and is capped at ±40 dB so a silent file isn't amplified
    into noise.
  - `to_wav(..., normalize=True)` — applies the gain as a single `volume=NdB`.
- `scribe/jobs.py` — a `plan()` helper, then a **fallback**: zero chunks from
  ≥1 s of audio retries 15 dB lower before believing it. Still nothing raises
  `AudioError`, so the job **fails visibly** instead of writing an empty file.
- `scribe/config.toml` + `settings._DEFAULTS` — `[chunking].normalize = true`.

**Key design decision — one constant gain, not dynamic normalisation.**
`dynaudnorm` or `loudnorm` would raise the noise floor during pauses, and the
pauses are exactly what the chunk planner cuts on and what becomes paragraph
breaks. A single constant scale keeps quiet passages proportionally quiet.
Verified: on two browser recordings, chunk counts and paragraph breaks are
**identical** before and after (11 chunks/6 breaks, 8 chunks/2 breaks).

**How to use / test it.** Automatic; `normalize = false` disables it. Five unit
tests (`test_gain_*`).

Measured on real recordings:

| Recording | Before | After |
|---|---|---|
| −56 dB note | 0 chunks, **0 words** | 1 chunk, **21 words** |
| −39 dB note | 13.2 s of 22 s | 22 s of 22 s, same 22 words |
| −22 dB browser | 8 chunks, 2 breaks | unchanged |

**Caveats**

- Two ffmpeg passes over the source now (measure, then convert). Cheap, but it
  is extra I/O on a long file.
- Peak-limited gain means one loud cough caps the lift for a whole quiet
  recording.
- It corrects level, not the cause. If the app is consistently recording at
  −50 dB, the macOS input level for that microphone is probably too low.

---

## 14. Microphone selection (Bluetooth and USB headsets)

**What it does.** You can record from any input device — a Bluetooth headset, a
USB mic — chosen inside the app, without changing your system-wide default. If
the device disappears mid-recording, you get told and keep what was captured.

**Why.** `AVAudioRecorder`, used previously, always records from the *system
default* input and offers no way to choose. Using a headset meant changing the
default in System Settings, which redirects every other app on the machine too.

**How it's implemented**

- `ScribeApp/Sources/Scribe/Recorder.swift` — rebuilt on `AVCaptureSession` +
  `AVCaptureAudioFileOutput`, which bind to a specific `AVCaptureDevice`.
  `audioSettings` still requests 16 kHz mono PCM, so device-native rates
  (Bluetooth headsets often run 16 or 24 kHz) are converted on the way in.
  `inputDevices()` enumerates, `selectedDevice()` resolves the saved
  `inputDeviceID`, falling back to the system default when a pinned device is
  absent.
- `AppDelegate` — a **Microphone** submenu, rebuilt in `menuWillOpen` so
  hot-plugged devices appear; the recording status line names the device in use.

**Two consequences worth knowing.**

*Stopping became asynchronous.* `AVCaptureAudioFileOutput.stopRecording()`
returns before the file is flushed, so the upload moved to an `onFinish`
callback driven by the recording delegate. Reading the file straight after
`stop()` would get a truncated WAV.

*Disconnection is now handled.* `AVCaptureDeviceWasDisconnected` stops the
session, alerts, and still submits the partial recording — previously a headset
powering off would have ended the recording with no indication.

**How to use / test it.** Menu → **Microphone** → pick a device.

**No automated tests** (Swift has no test target). Verified by capturing from
both devices on this machine — a SteelSeries Arctis Nova 7 wireless headset and
the built-in mic — and confirming each produced `pcm_s16le, 16000 Hz, 1 ch`.

**Caveats**

- Bluetooth mics in headset mode are typically low bandwidth; Whisper handles
  16 kHz natively so this matters less than it would elsewhere, but audio
  quality still caps accuracy.
- A saved device that is disconnected shows a disabled "(saved device not
  connected)" row rather than silently reverting, but recording will use the
  system default until it returns.
- Sleep mid-recording remains unhandled.
