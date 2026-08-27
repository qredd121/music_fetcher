# music-fetcher

A CLI that wraps [slskd](https://github.com/slskd/slskd) (Soulseek) and [beets](https://beets.io) to download music and manage your `~/Music` library.

## How it works

- **slskd** runs in Docker and talks to the Soulseek network. music-fetcher controls the container and drives its REST API.
- **beets** handles tagging and library organization. music-fetcher shells out to `beet`, layering its own settings on top of your existing beets configuration and library.
- A **SQLite job queue** in the state directory tracks every download so it survives restarts.

## Requirements

- Python 3.12+
- Docker with the Compose plugin
- `uv`

## Setup

```sh
uv sync
cp config.example.toml ~/.config/music-fetcher/config.toml
```

Secrets live in `.env` (never in the TOML):

```
SLSKD_SLSK_USERNAME=...
SLSKD_SLSK_PASSWORD=...
SLSKD_API_KEY=...
SLSKD_WEB_USERNAME=...
SLSKD_WEB_PASSWORD=...
```

Then verify everything:

```sh
music-fetcher doctor
music-fetcher daemon up
```

## Configuration

Settings come from `~/.config/music-fetcher/config.toml`; secrets and a few paths can be overridden by environment variables (see `.env`). Precedence: environment variable > TOML > built-in default. See `config.example.toml` for every key.

## Commands

### Daemon

```sh
music-fetcher daemon up          # start slskd
music-fetcher daemon status      # container + API health
music-fetcher daemon logs -f     # follow logs
music-fetcher daemon down        # stop slskd
```

### Search and download

```sh
music-fetcher search "artist album"          # interactive browse only
music-fetcher dl album "aphex twin scarts"   # MusicBrainz-resolved album
music-fetcher dl album --mbid <release-id>   # skip the lookup
music-fetcher dl track "song title"          # single track
```

By default the candidate picker is interactive. Pass `--yes` to auto-pick the
best source, or `--no-wait` to queue without downloading immediately. If the
preferred format (e.g. FLAC) is unavailable, you are asked before falling back.

### Queue

```sh
music-fetcher jobs list          # active jobs (--all for finished)
music-fetcher jobs show <id>
music-fetcher jobs run           # drain the queue, resuming interrupted jobs
music-fetcher jobs retry <id>
music-fetcher jobs cancel <id>
music-fetcher jobs rm <id>
```

### Import

Downloads are staged, then imported explicitly:

```sh
music-fetcher import              # import all completed downloads
music-fetcher import <id>         # import one job
music-fetcher import --yes        # non-interactive autotag
music-fetcher import --staged     # also sweep orphaned folders in the downloads dir
```

Albums that beets cannot confidently tag are moved to a quarantine directory in
the state dir; run `import` again to retry them.

### Library

```sh
music-fetcher lib adopt          # register existing ~/Music with beets (as-is, in place)
music-fetcher lib adopt --autotag
music-fetcher lib stats
music-fetcher lib dupes
music-fetcher lib verify         # confirm every item still exists on disk
music-fetcher lib art            # fetch missing album art
music-fetcher lib beet ls ...    # run any beet command against the library
```

## Development

```sh
uv run ruff check src
uv run pytest
```
