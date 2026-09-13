# scribe

Rant into a microphone for an hour, pause whenever you need to think, and get
back something you'd actually read. A local web UI over whisper.cpp /
mlx-whisper / parakeet-mlx.

Single user, single machine. It serves on `localhost`, transcribes with a model
on your own disk, and writes markdown into a folder you choose. There is no
account, no server and no database anywhere in it, and no audio leaves the Mac.

Nothing leaves it at all, in fact: the webfont is bundled rather than fetched
from Google, so the UI makes zero third-party requests and works with the
network off. The only time anything is downloaded is the first use of a new
model, from Hugging Face.

Working on the code? **Read the privacy rule first:** don't read any part of
someone's transcripts without their explicit permission — see
[CLAUDE.md](CLAUDE.md) and the top of [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The modules, and one recording traced end to end |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Common changes, conventions, and 20 gotchas |
| [docs/MENUBAR_APP.md](docs/MENUBAR_APP.md) | The Swift app: building it, changing it, debugging it |
| [docs/FEATURES.md](docs/FEATURES.md) | What was added since the original drop, and why |

## Setup

```bash
brew install ffmpeg                 # the one thing you install yourself
./start.sh
```

**Prerequisites:** macOS on Apple Silicon (the MLX backends are Metal-only),
Homebrew, and Python 3.11+ available somewhere — `start.sh` finds a suitable
interpreter itself. ffmpeg is the only thing you install by hand.

That's it. `start.sh` builds the virtualenv if it's missing, installs
dependencies if `requirements.txt` has changed since last time, starts the
server and opens the UI. Run it from anywhere, run it as often as you like;
it's the everyday command. `SCRIBE_PORT=9000 ./start.sh` to move it.

It picks a Homebrew Python 3.11+ deliberately rather than whatever `python3`
happens to be — MLX needs 3.11+, and on a Mac with Anaconda installed the bare
name usually resolves there, which is a rockier path for MLX wheels.

## Starting and stopping

Three ways in, all the same thing underneath:

| | |
|---|---|
| **Scribe.app** | The menu bar app. Records, transcribes and opens the result. Starts the engine itself. |
| **Start Scribe.command** | Browser-only: double-click, runs in Terminal, opens the web UI. |
| `./start.sh` | From a shell. `run.sh` is the bare uvicorn line under it. |

**Scribe.app is the everyday way in.** It puts a waveform in the menu bar and
owns the Python engine as a child process, so there is no separate server to
start or stop:

- **Start / Stop Recording** — captures at 16 kHz mono straight to disk
- **Model** — pick a preset; **Recent** — reopen the last five transcripts
- **Microphone** — record from any input, including Bluetooth and USB headsets,
  without changing your system-wide default
- **Open Transcript When Ready** — opens the cleaned `.md` the moment a job lands
- **Open Transcripts In** — which app that uses. Scoped to Scribe, so it doesn't
  disturb the system-wide handler you use for `.md` files in code
- **Launch at Login**, and **Quit**, which stops the engine with it

To stop: **Quit Scribe** in the menu, or **Quit** in the web UI. Both are
graceful and safe mid-transcription — the job resumes from its checkpoint.

The UI opens in an app window — no tabs, no URL bar, its own Dock entry — when
Chrome, Brave or Edge is installed, and falls back to an ordinary tab
otherwise. `SCRIBE_BROWSER_WINDOW=0` forces the plain tab.

| Variable | Default | Effect |
|---|---|---|
| `SCRIBE_PORT` | `8765` | Port to serve on |
| `SCRIBE_BROWSER_WINDOW` | `1` | `0` opens an ordinary tab rather than an app window |
| `SCRIBE_NO_OPEN` | unset | Set to anything to start without opening a browser at all |

Those are the only environment variables, and there is no `.env` file —
everything else lives in `scribe/config.toml`.

It stays a real browser rather than an embedded webview on purpose: the
microphone is the entire point here, and `getUserMedia` inside a `WKWebView`
needs the bundle signed and entitled before macOS will grant it, while the
permission you already gave `localhost` just keeps working.

**Keep this folder out of `~/Downloads`, `~/Desktop` and `~/Documents`.** Those
are TCC-protected, and an unsigned app launched from Finder cannot execute
anything inside them — `Scribe.app` fails with "Operation not permitted" before
it starts. A folder directly in your home directory is fine.

Use `localhost`, not your LAN IP. Browsers only expose the microphone in a
secure context, and `localhost` counts as one while `http://192.168.x.x` does
not — mic recording silently fails over the network address.

## What comes out

Every finished session writes two files into `~/Documents/scribe` (change it in
`scribe/config.toml`):

```
2026-09-03-1442-rant.md        cleaned — filler stripped, the one you read
2026-09-03-1442-rant.raw.md    verbatim — every um and false start
```

The pair is the point. Because the raw file always exists, the cleaning is free
to be aggressive — nothing it removes is ever actually lost, so you never have
to trade "readable" against "what I really said".

Both are paragraphed on your thinking pauses, so the transcript arrives shaped
like the thinking that produced it rather than as four hundred lines of
fragments. Front matter carries the duration, the model, and a path back to the
audio.

## Choosing models

Model presets live in `scribe/presets.toml`; everything else about how the app
behaves is in `scribe/config.toml`. They're separate files because they change
for different reasons — you settle on a model and stop touching it, while the
filler list and vocabulary get edited as you learn how you actually talk.

```toml
[presets.accurate]
label = "Whisper large-v3-turbo"
backend = "mlx-whisper"
model = "mlx-community/whisper-large-v3-turbo"
language = "en"
```

Three backends. `mlx-whisper` is the default and the fastest Whisper path on
Apple Silicon. `parakeet-mlx` is faster still and scores better on English, but
is English-only and takes no prompt. `whisper-cpp` shells out to `whisper-cli`
and runs any local GGML file, including community fine-tunes — slower to start
since it reloads the model per chunk, but it has no Python dependencies at all.

### Teach it your vocabulary

Whisper will reliably mangle proper nouns — your project names, your tools, the
people you work with. `config.toml` has a `vocabulary` list that biases the
decoder toward the right spelling. For ranting about your own work it's the
single biggest accuracy win available, and it costs nothing at runtime.

The same mechanism is what makes a verbatim version possible at all: Whisper
imitates the style of whatever it's primed with, and was trained on tidied-up
subtitles, so left alone it quietly removes your disfluencies as it decodes.
Seeding the prompt with a deliberately messy sentence pulls it back toward what
you actually said.

## How it works

```
ingest ──▶ pause detection ──▶ speech-only chunk plan ──▶ backend ──▶ stitch ──▶ clean ──▶ write
```

**Ingest.** ffmpeg decodes anything into 16 kHz mono PCM — Whisper's native
format, so no resampling happens inside the model.

**Pause detection.** ffmpeg's `silencedetect` filter stands in for a VAD model.
Silero would be more accurate, but it pulls in torch for what is fundamentally
a question about loudness.

**Chunk plan.** Chunks are built out of *speech only*; silence is skipped rather
than sliced. See below — this is the part that matters most.

**Backend.** One method — wav in, timestamped segments out. That contract is
the entire "models of your choice" feature.

**Stitch.** Each chunk's timestamps get its start offset added back. Nothing is
renumbered, so every timestamp still lines up with the original recording.

**Clean.** Filler removal, in tiers by how much is lost when the guess is wrong.

## The pause problem

The obvious design — cut the timeline at the midpoint of each pause — is right
for the half-second gaps between sentences and badly wrong for the long ones.
Stop for forty seconds to work something out and that approach hands the model
chunks that are *entirely room tone*:

```
chunk 1:  25.0- 50.0 (25.0s)  speech  20.0%   <-- mostly silence
chunk 2:  50.0- 75.0 (25.0s)  speech   0.0%   <-- ALL SILENCE
```

Room tone is precisely the input Whisper hallucinates on — it was trained on
subtitle tracks, so silence decodes to "Thanks for watching!". You'd get that
spliced into the middle of your notes with no way to tell it wasn't you.

So silence is skipped instead. The same timeline now plans as:

```
chunk 0:   0.0- 25.0 (25.0s)  speech 100.0%
chunk 1:  25.0- 30.2 ( 5.2s)  speech  95.2%   [paragraph break before]
chunk 2:  74.8- 99.8 (25.0s)  speech  99.0%
```

81 seconds of audio sent to the model instead of 133. The hallucination window
is gone, and on a session where you spend half the time thinking it's also most
of a 2× speedup.

One threshold does two jobs. A gap under `paragraph_gap` is speech rhythm, so
the audio either side stays in one chunk. A gap over it means you stopped to
think — which ends the chunk *and* starts a new paragraph. The structure is
free: the planner already had to find the pauses in order to skip them.

## Other things it handles that naive scripts don't

- **Repetition loops.** The decoder latches onto a phrase and emits it forever.
  Two layers: the temperature-fallback ladder catches it inside the model, and
  `_drop_repeats` catches what leaks across chunk boundaries. It only considers
  repeats over 25 characters — without that floor it eats ordinary speech, since
  thinking out loud is full of repeated short beats ("Right." "Okay." "Yeah.").
- **Error cascade.** `condition_on_previous_text=False` / `--no-context` stops
  one bad chunk from poisoning every chunk after it. On long audio this is the
  single highest-value flag in the whole pipeline.
- **Checkpointing.** Every chunk's result is written to
  `~/.scribe/jobs/<id>/checkpoint.json` as it completes, and **Resume** on a
  failed job re-queues that same job id — so it lands on the same directory and
  picks up where it stopped. Dying at minute 95 of a two-hour file costs you
  minute 95, not the file.
- **Disk.** The decoded PCM working copy (~115 MB/hour) is deleted when a job
  finishes. Your original recording stays for playback, and the transcript's
  timestamps are valid against it, because decoding never shifts time.

## Recording

The browser's `MediaRecorder` emits a blob every five seconds; each goes
straight to the server and is appended to one file. The tab never holds the
whole recording, so session length is bounded by disk rather than by memory.

There's a **Pause** button, but you rarely need it — silence is skipped either
way, and a long think is *wanted* in the recording since that's what becomes a
paragraph break. Pause is for stepping away, so five minutes of making coffee
never reaches the disk.

## Layout

```
scribe/
  audio.py         ffmpeg ingest, silence detection, speech-only chunk planning
  backends.py      the three model backends behind one Protocol
  clean.py         filler removal, paragraph assembly
  jobs.py          worker thread, checkpointing, export formats
  settings.py      config loading
  app.py           FastAPI routes
  presets.toml     model configuration
  config.toml      everything else
  static/index.html
tests/
  test_scribe.py   chunk planner, cleaner, repeat filter, timecodes
start.sh           everyday launcher: sets up whatever is missing, then serves
run.sh             bare uvicorn, assumes an active venv
Scribe.app         double-click launcher; delegates to start.sh
Start Scribe.command   the same, but in Terminal so you can watch it
```

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q
```

25 tests, about 0.02s. They cover pure logic only — the chunk planner, the
cleaner, the repeat filter, timecode formatting — so they need neither ffmpeg
nor a model, and are fast enough to leave running in a loop while you work.

## Caveats

What has actually been run, so you know which parts are load-bearing and which
are still theory:

**Proven.** The full pipeline end to end against real audio and a real model —
browser recording, ingest, silence detection, speech-only planning, slicing,
`MlxWhisperBackend` (whisper-tiny, large-v3 and large-v3-turbo), stitching,
dedup, paragraphing, both markdown files, every export format, retry from a
checkpoint, delete, and restart persistence.

**Never executed against a real model:** `WhisperCppBackend` and
`ParakeetMlxBackend`. Parakeet is the likelier of the two to break —
parakeet-mlx's result shape has moved between releases, so it reads fields
defensively; if it returns empty, check what `.transcribe()` actually gives you
and patch that one method. For whisper.cpp, the JSON parser (`offsets.from` /
`offsets.to`, milliseconds) and the `--prompt` flag are the parts to check
first.

**Untested:** Safari. It records MP4 rather than WebM, and appending MP4
fragments is not equivalent to appending WebM clusters. The server names the
file from the `Content-Type` the browser sends, which should handle it, but no
one has tried. Chrome, Brave and Edge are the verified path.

## License

MIT — see [LICENSE](LICENSE).

The bundled Archivo webfont is a separate work under the SIL Open Font License
1.1; see [scribe/static/fonts/README.txt](scribe/static/fonts/README.txt).

## Worth adding later

- Speaker diarization (pyannote) — the one genuinely hard MacWhisper feature
  left out here
- A transcript editor with audio-synced playback
- System audio capture, for transcribing calls rather than yourself
- Word-level timestamps via DTW, if you start caring about subtitle precision
