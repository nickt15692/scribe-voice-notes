# Working in this repo

## Transcripts are private — ask first, every time

Scribe exists to capture things people say out loud and don't intend to share.
Before reading **any part** of the user's transcripts — a single line, a
header, a word count, a "quick spot check" — ask for explicit permission and
wait for a yes. Permission covers what you asked about; ask again for anything
beyond it.

This applies to every route to transcript content, not just opening the files:

- The output folder (`[output].folder` in `scribe/config.toml`, default
  `~/Documents/scribe`) — `.md` and `.raw.md`, front matter included
- `~/.scribe/jobs/*/job.json` and `checkpoint.json` — both store transcript text
- `~/.scribe/uploads/` and `~/.scribe/capture/` — the recordings; transcribing
  or playing one reveals what was said
- The API: `/api/jobs/{id}/text`, `/transcript`, `/export`
- Any script run over transcript text, even one that only prints counts
- `test/` in this repo, which holds exported transcripts and is gitignored

Test with synthetic data instead: `say -o file.aiff "..."` for audio, invented
sentences for the cleaner. The test suite already works this way.

Not covered: reading a job's status, or confirming a file exists without
opening it; running `./.venv/bin/python -m pytest tests/ -q`. If unsure whether
something counts, ask.

Examples elsewhere in `docs/` that read transcript content — such as the `curl`
calls against `/text` and `/export` in `docs/FEATURES.md` — are for the owner of
the installation. They don't override this rule.

## Everything else

See `docs/DEVELOPMENT.md` for conventions and gotchas, and
`docs/ARCHITECTURE.md` for how the pieces fit together.
