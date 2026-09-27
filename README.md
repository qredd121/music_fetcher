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

### Excluding directories

Set `paths.exclude` to a list of directories (relative to `paths.music`) that
should be completely ignored by every library operation — `adopt`, `stats`,
`dupes`, `verify`, and `art`:

```toml
[paths]
exclude = ["I/Incelcore"]
```

Exclusions are enforced as beets `ignore` globs, so `adopt` skips those trees.
Items that were already adopted stay in the beets database until you rebuild it;
after changing `exclude`, run:

```sh
music-fetcher lib adopt --rebuild
```

`--rebuild` backs up the beets database to `library.db.bak`, deletes it, and
re-imports `~/Music` in place (skipping excluded directories), so the database
never contains excluded items. The raw `lib beet ...` passthrough is not
filtered.

> `adopt` is strictly in place: it never copies, moves, or rewrites tags. The
> `import` command still moves staged downloads into `~/Music`, filing them
> under their first-letter bucket (e.g. `I/Incelcore/...`).

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
music-fetcher search "artist album"          # Soulseek candidates as JSON
music-fetcher dl album "aphex twin scarts"   # MusicBrainz-resolved album
music-fetcher dl album --mbid <release-id>   # skip the lookup
music-fetcher dl track "song title"          # single track
```

`search` prints JSON to stdout (no interactive picker). Each result is one
album-folder candidate with a stable `id`, ranked best-first:

```json
{
  "query": "you and i the curtain falls",
  "count": 3,
  "results": [
    {
      "id": "cbe89b6b9f65",
      "username": "web-graffiti",
      "folder": "You and I - The Curtain Falls [US 1999 CD]",
      "remote_dir": "music\\...\\The Curtain Falls",
      "formats": ["FLAC"],
      "file_count": 8,
      "meets_preference": true,
      "relevance": 1.0,
      "files": [{"filename": "...", "size": 123, "extension": "FLAC"}]
    }
  ]
}
```

By default the `dl` candidate picker is interactive. Pass `--yes` to auto-pick
the best source, or `--no-wait` to queue without downloading immediately. If the
preferred format (e.g. FLAC) is unavailable, you are asked before falling back.

#### Agent-friendly flow

`dl album` is **MusicBrainz-first**: it resolves the release, then searches
Soulseek on the canonical `artist title`. Use `--list` to get JSON candidates
(each with an `id`) without downloading, then re-run with `--result-id` to grab
a specific source non-interactively:

```sh
# discover sources for a resolved album (JSON on stdout, status on stderr)
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" --list

# download a specific source by id
id=$(music-fetcher dl album --artist "You and I" --album "The Curtain Falls" \
       --list | jq -r '.results[0].id')
music-fetcher dl album --artist "You and I" --album "The Curtain Falls" \
       --result-id "$id" --no-wait
```

`dl track` supports the same `--list` / `--result-id` flags. On success the id
path prints the created job as JSON (`{"job_id", "state", "username",
"file_count", "remote_dir"}`); an unknown id prints `{"error":
"result_id_not_found", ...}` and exits non-zero. stdout stays pure JSON on these
paths — all human-facing status goes to stderr.

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
music-fetcher lib adopt --rebuild   # wipe + regenerate the DB, applying exclusions
music-fetcher lib stats
music-fetcher lib dupes
music-fetcher lib verify         # confirm every item still exists on disk
music-fetcher lib art            # fetch missing album art
music-fetcher lib beet ls ...    # run any beet command against the library
```

## Streaming with Navidrome

The Docker Compose file also runs [Navidrome](https://www.navidrome.org/), a
Subsonic-compatible streaming server, alongside slskd. It mounts `~/Music`
read-only and keeps its own database, playlists, and art cache in
`./data/navidrome`, so it never modifies your beets-managed files.

```sh
music-fetcher daemon up          # starts slskd and navidrome together
docker logs navidrome            # follow the library scan
```

Navidrome is served over HTTPS at `https://<device-ip>:8443`, reverse-proxied by
the host's nginx (many Subsonic clients require HTTPS). It is not published on
plain HTTP; the container only exposes port 4533 on the internal Docker network.
Open the URL and create the admin account on first load. The certificate is
self-signed, so accept the browser/client warning or import the server CA
(`ca-cert.pem`) on your devices.

The library is rescanned every hour (`ND_SCANSCHEDULE`); trigger a manual rescan
from the web UI after importing new music. `music-fetcher daemon down` stops both
containers.

### nginx reverse proxy

The TLS termination lives in a separate nginx stack. To reach the `navidrome`
container by name, nginx joins the `music-fetcher_default` network (declared
`external: true` in its compose file) and serves an HTTPS `server` block on port
`8443` that proxies to `http://navidrome:4533`.

## Development

```sh
uv run ruff check src
uv run pytest
```
