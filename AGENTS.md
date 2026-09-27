# AGENTS.md

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
- beets owns this layout. NEVER move, rename, or retag files under `~/Music` by
  hand. Let `import` / `lib adopt` do it.

## Non-interactive contract

For automation, always use the JSON/`--yes` paths:

- `search`, `dl … --list`, `dl … --result-id`, `import --yes`,
  `lib adopt --rebuild --yes`.
- On these paths stdout is pure JSON and all human-facing status goes to
  stderr. Parse stdout only.
- Never invoke the interactive `dl` / `import` pickers in automation.

## Canonical workflow

```sh
# 1. Resolve + discover sources (JSON on stdout)
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" --list

# 2. Pick an id, queue it without blocking
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" \
  --result-id "<id>" --no-wait

# 3. Drain the download queue
music-fetcher jobs run

# 4. File staged downloads into ~/Music
music-fetcher import --yes
```

- `dl track` mirrors the same `--list` / `--result-id` flags.
- An unknown id prints `{"error": "result_id_not_found", ...}` and exits
  non-zero. Handle it.

## Queue & recovery

- The job queue is SQLite in the state dir and survives restarts.
- `jobs run` resumes interrupted/queued jobs.
- Albums beets cannot confidently tag are moved to a quarantine dir; re-run
  `import` to retry them.
- Other commands: `jobs list` (`--all`), `jobs show/retry/cancel/rm <id>`.

## Guardrails / footguns

- `lib adopt --rebuild` deletes and regenerates the beets DB (backup saved to
  `library.db.bak`) and applies exclusions. It requires `--yes` in
  non-interactive mode.
- `lib beet …` is a raw passthrough and is NOT filtered by `paths.exclude`.
- `beets_config.yaml` in the state dir is generated on every run. Do not
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
