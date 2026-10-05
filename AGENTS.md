# AGENTS.md


## If you are about to change some code

no comments in code at all
short and consice commit messages



Operating manual for automation/agents working in this repo. Terse and
imperative by design; see `README.md` for setup walkthroughs, the full command
reference, and JSON schemas.

## Purpose

`music-fetcher` is a CLI that wraps slskd (Soulseek) and beets to download music
and manage the `~/Music` library. It is built to be driven non-interactively.

## Prime directive: library layout

New music MUST land as:

```
~/Music/<LETTER>/<Artist>/<Year - Album>/<Track - Title>.<ext>
```

- `<LETTER>` is the first letter of the album artist, uppercased.
- Non-alphanumeric first characters bucket under `#`.
- Non-ASCII names are transliterated (via unidecode) to pick the bucket, e.g.
  `Оргазм Нострадамуса` -> `O`, `Янка Дягилева` -> `I`.
- Singletons land as `~/Music/<LETTER>/<Artist>/<Title>.<ext>`.
- beets owns this layout. NEVER move, rename, or retag files under `~/Music` by
  hand. Let `import` / `lib adopt` do it.
- There is no MusicBrainz/online lookup. Tags come from `dl` args and
  `tags set` overrides, then file tags, then folder/file names. beets runs
  with autotag off (`-A`).

## Non-interactive contract

For automation, always use the JSON/`--yes` paths:

- `search`, `dl … --list`, `dl … --result-id`, `import --yes`, `tags show`,
  `tags set … --dry-run`, `tags set … --yes`, `lib adopt --rebuild --yes`.
- On these paths stdout is pure JSON and all human-facing status goes to
  stderr. Parse stdout only.
- Never invoke the interactive `dl` / `import` pickers in automation.

## Canonical workflow

```sh
# 1. Discover sources (JSON on stdout); --artist/--album required, --year recommended
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" --year 1999 --list

# 2. Pick an id, queue it without blocking
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" --year 1999 \
  --result-id "<id>" --no-wait

# 3. Drain the download queue
music-fetcher jobs run

# 4. File staged downloads into ~/Music
music-fetcher import --yes
```

- `dl album` args are written to the files on import, overriding uploader tags.
- `dl track` mirrors the same `--list` / `--result-id` flags and takes
  optional `--artist` / `--title`.
- An unknown id prints `{"error": "result_id_not_found", ...}` and exits
  non-zero. Handle it.

## Queue & recovery

- The job queue is SQLite in the state dir and survives restarts.
- `jobs run` resumes interrupted/queued jobs.
- Folders with missing/inconsistent tags (album: albumartist, album, year,
  track, title) are moved to `<state>/quarantine/` with a `.mf-plan.json`.
  Plain `import` retries all of them.

## Fixing quarantined tags (approval required)

```sh
music-fetcher tags show "<folder name|path|job id>"        # inspect
music-fetcher tags set "<target>" --year 1991 --dry-run    # propose, show diff to user
music-fetcher tags set "<target>" --year 1991 --yes        # ONLY after user approves
```

- Never run `tags set --yes` without the user's explicit approval of the
  dry-run diff.
- Per-file fields: `--file <index|relpath> --title … --track … --disc …`, or
  bulk via `--meta '{"album": {...}, "files": {"<index>": {...}}}'`.
- Overrides persist in `.mf-meta.json` in the folder.
- Other commands: `jobs list` (`--all`), `jobs show/retry/cancel/rm <id>`.

## Guardrails / footguns

- `lib adopt --rebuild` deletes and regenerates the beets DB (backup saved to
  `library.db.bak`) and applies exclusions. It requires `--yes` in
  non-interactive mode.
- `lib beet …` is a raw passthrough and is NOT filtered by `paths.exclude`.
- `config.yaml` in the state dir is generated on every run. Do not
  hand-edit it; change `~/.config/music-fetcher/config.toml` instead.

## Environment

- Python 3.12+. Run everything via `uv run …`.
- Docker Compose runs the daemons. `music-fetcher daemon up` / `down` controls
  slskd AND Navidrome together.
- Secrets live only in `.env` (never in TOML, never committed).
- Config precedence: environment variable > TOML > built-in default.

## Verify your work

```sh
uv run ruff check src
uv run pytest
music-fetcher doctor
```

## Infra note

Navidrome mounts `~/Music` read-only and is served over HTTPS at
`https://<device-ip>:8443` via an external nginx reverse proxy. It is not
published on plain HTTP; do not rely on port 4533 from outside the Docker
network.

## Don'ts

- Don't edit, move, or rename files under `~/Music` manually.
- Don't commit `.env`, `data/`, or `*.db` (they are gitignored).
- Don't run interactive commands in automation.
- Don't hand-edit the generated `beets_config.yaml`.
