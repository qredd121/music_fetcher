from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer
from rich.progress import Progress
from rich.table import Table

from . import __version__, daemon
from . import doctor as doctor_mod
from .config import ensure_dirs, load_config
from .jobs import ACTIVE_STATES, Runner
from .jobs.db import JobStore
from .musicbrainz import Release, get_release, search_releases
from .selection import (
    ScoredCandidate,
    best_single_file,
    common_remote_dir,
    meets_format_preference,
    score_candidates,
)
from .slskd import SlskdClient, SlskdError
from .ui import (
    confirm,
    console,
    format_size,
    pick_index,
    print_candidate_table,
)

app = typer.Typer(
    name="music-fetcher",
    help="Download music from Soulseek via slskd and manage a beets library.",
    no_args_is_help=True,
    add_completion=True,
)


@app.callback()
def _main(
    log_level: str = typer.Option(
        None, "--log-level", help="Override log level (DEBUG, INFO, WARNING, ERROR)."
    ),
) -> None:
    from .logging_setup import configure_logging

    configure_logging(log_level)


def _emit_json(payload: object) -> None:
    """Write a JSON document to stdout for machine/agent consumption."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _err_console():
    """A Rich console bound to stderr, so stdout stays pure JSON."""
    from rich.console import Console

    return Console(stderr=True)


def _search_candidates(
    config, search_text: str, *, artist: str = "", album: str = "",
    expected_tracks: int | None = None,
) -> list[ScoredCandidate]:
    client = SlskdClient(config.slskd)
    results = client.search(
        search_text,
        timeout=config.search.timeout,
        poll_interval=config.search.poll_interval,
    )
    return score_candidates(
        results,
        config.download.format_preference,
        expected_tracks=expected_tracks,
        artist=artist,
        album=album,
    )


daemon_app = typer.Typer(help="Manage the slskd docker container.", no_args_is_help=True)
app.add_typer(daemon_app, name="daemon")

jobs_app = typer.Typer(help="Inspect and control the download queue.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


def _store():
    from .config import load_config
    from .jobs import JobStore

    return JobStore(load_config().paths.state / "jobs.db")


_STATE_STYLES = {
    "queued": "cyan",
    "downloading": "yellow",
    "downloaded": "green",
    "imported": "bright_green",
    "failed": "red",
    "quarantined": "magenta",
    "cancelled": "dim",
}


@jobs_app.command("list")
def jobs_list(
    show_all: bool = typer.Option(False, "--all", help="Include finished jobs."),
) -> None:
    store = _store()
    rows = store.list() if show_all else store.list(states=ACTIVE_STATES)
    if not rows:
        console.print("queue is empty")
        return
    table = Table(title="Jobs")
    table.add_column("ID")
    table.add_column("Kind")
    table.add_column("Query")
    table.add_column("State")
    table.add_column("User")
    table.add_column("Updated")
    for job in rows:
        style = _STATE_STYLES.get(job.state, "white")
        table.add_row(
            job.id[:8], job.kind, job.query, f"[{style}]{job.state}[/{style}]",
            job.username or "-", job.updated_at,
        )
    console.print(table)


@jobs_app.command("show")
def jobs_show(job_id: str) -> None:
    job = _resolve_job(job_id)
    info = {
        "id": job.id,
        "kind": job.kind,
        "query": job.query,
        "state": job.state,
        "mbid": job.mbid,
        "username": job.username,
        "folder": job.folder,
        "expected_files": job.expected_files,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "payload": job.payload,
    }
    console.print_json(json.dumps(info, indent=2))


def _resolve_job(prefix: str):
    store = _store()
    matches = [job for job in store.list() if job.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        console.print(f"[red]no job matching {prefix}[/red]")
    else:
        console.print(f"[red]ambiguous prefix {prefix}[/red]")
    raise SystemExit(1)


@jobs_app.command("retry")
def jobs_retry(job_id: str) -> None:
    store = _store()
    job = _resolve_job(job_id)
    payload = {**job.payload, "attempts": 0}
    store.update_state(job.id, "queued", error=None, set_fields={"payload": payload})
    console.print(f"[green]{job.id[:8]} re-queued[/green]")


@jobs_app.command("cancel")
def jobs_cancel(job_id: str) -> None:
    store = _store()
    job = _resolve_job(job_id)
    username = job.payload.get("username")
    remote_dir = job.payload.get("remote_dir")
    if username and remote_dir:
        try:
            client = SlskdClient(load_config().slskd)
            transfers = client.transfers()
            files = Runner._match_files(transfers, username, remote_dir)
            for file in files:
                client.cancel_transfer(username, file.get("filename", ""))
        except SlskdError as error:
            console.print(f"[yellow]remote cancel skipped:[/yellow] {error}")
    store.update_state(job.id, "cancelled")
    console.print(f"{job.id[:8]} cancelled")


@jobs_app.command("rm")
def jobs_rm(job_id: str) -> None:
    store = _store()
    job = _resolve_job(job_id)
    store.delete(job.id)
    console.print(f"{job.id[:8]} removed")


@jobs_app.command("run")
def jobs_run() -> None:
    config = load_config()
    store = JobStore(config.paths.state / "jobs.db")
    runner = Runner(config, store)
    pending = store.list(states=("queued", "downloading"))
    if not pending:
        console.print("nothing queued")
        return
    with Progress() as progress:
        task = progress.add_task("downloading", total=None)
        current: list[str] = [""]

        def on_progress(done: int, total: int) -> None:
            label = f"{current[0]} {done}/{total}"
            progress.update(task, total=total or 1, completed=done, description=label)

        def wrap_run():
            while True:
                pending_now = store.list(states=("downloading", "queued"))
                job = next(
                    (j for j in pending_now if j.state == "downloading"),
                    pending_now[0] if pending_now else None,
                )
                if job is None:
                    break
                current[0] = job.query[:40]
                runner.run_job(job, progress=on_progress)

        wrap_run()
    done_jobs = store.list(states=("downloaded", "failed"))
    for job in done_jobs[-len(pending):]:
        color = "green" if job.state == "downloaded" else "red"
        console.print(f"[{color}]{job.state}[/{color}] {job.query}")



@daemon_app.command("up")
def daemon_up() -> None:
    daemon.up()
    console.print("[green]slskd started[/green]")


@daemon_app.command("down")
def daemon_down() -> None:
    daemon.down()
    console.print("slskd stopped")


@daemon_app.command("status")
def daemon_status() -> None:
    services = daemon.ps()
    if not services:
        console.print("no containers running")
        return
    for service in services:
        name = service.get("Name") or service.get("Service") or "?"
        state = service.get("State") or "?"
        color = "green" if state == "running" else "red"
        console.print(f"[{color}]{state}[/{color}]  {name}")
    config = load_config()
    client = SlskdClient(config.slskd)
    api = "[green]reachable[/green]" if client.ping() else "[red]not reachable[/red]"
    console.print(f"API: {api}")


@daemon_app.command("logs")
def daemon_logs(
    follow: bool = typer.Option(False, "--follow", "-f"),
    tail: int | None = typer.Option(None, "--tail"),
) -> None:
    daemon.logs(follow=follow, tail=tail)


@app.command()
def doctor() -> None:
    """Check slskd, paths, beets and MusicBrainz connectivity."""
    config = load_config()
    results = doctor_mod.run_all_checks(config)
    failed = False
    for result in results:
        style = {"OK": "green", "WARN": "yellow", "FAIL": "red"}[result.status]
        if result.status == "FAIL":
            failed = True
        console.print(f"[{style}]{result.status:>4}[/{style}]  {result.name:<14} {result.detail}")
    raise SystemExit(1 if failed else 0)


@app.command()
def search(
    query: str = typer.Argument(..., help="Search text sent to slskd."),
    timeout: float | None = typer.Option(None, help="Seconds to wait for responses."),
    max_results: int = typer.Option(25, help="Max candidates to return."),
) -> None:
    """Search the Soulseek network; print album-folder candidates as JSON.

    Output is a single JSON document with a ``results`` array. Each result has a
    stable ``id`` that can be passed to ``dl ... --result-id`` to download it.
    """
    config = load_config()
    effective_timeout = timeout if timeout is not None else config.search.timeout
    config.search.timeout = effective_timeout

    # Seed relevance from the free-text query so ranking is meaningful.
    candidates = _search_candidates(config, query, album=query)
    candidates = candidates[:max_results]
    chain = config.download.format_preference

    _emit_json(
        {
            "query": query,
            "count": len(candidates),
            "results": [c.to_dict(chain) for c in candidates],
        }
    )


dl_app = typer.Typer(help="Search and download music.", no_args_is_help=True)
app.add_typer(dl_app, name="dl")

lib_app = typer.Typer(help="Manage the existing music library.", no_args_is_help=True)
app.add_typer(lib_app, name="lib")


def _resolve_job(prefix: str):
    store = _store()
    matches = [job for job in store.list() if job.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        console.print(f"[red]no job matching {prefix}[/red]")
    else:
        console.print(f"[red]ambiguous prefix {prefix}[/red]")
    raise SystemExit(1)


@app.command("import")
def import_(
    job_id: str = typer.Argument(None, help="Job prefix; defaults to all downloaded."),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive autotag."),
    staged: bool = typer.Option(
        False, "--staged", help="Also import orphaned folders in the downloads dir."
    ),
) -> None:
    """Import staged downloads into the beets library."""
    from .beetsbridge import import_path

    config = load_config()
    store = _store()
    jobs = [_resolve_job(job_id)] if job_id else store.list(states=("downloaded",))
    quarantined = [] if job_id else store.list(states=("quarantined",))

    sources: list[tuple[object, Path, bool]] = []
    for job in jobs:
        sources.append((job, Path(job.folder or ""), job.kind == "track"))

    if staged:
        imported_dirs = {Path(job.folder or "").name for job in store.list()}
        for entry in sorted(config.paths.downloads.iterdir()):
            if entry.is_dir() and entry.name not in imported_dirs:
                sources.append((None, entry, False))

    if not sources and not quarantined:
        console.print("nothing to import")
        return

    interactive = not yes and sys.stdout.isatty()
    handled_dirs: set[str] = set()
    for job, source, singletons in sources:
        if not source.exists():
            if job is not None:
                console.print(f"[red]{job.id[:8]} staged path missing: {source}[/red]")
            continue
        handled_dirs.add(source.name)
        label = job.id[:8] if job is not None else source.name
        console.print(f"importing {label} [dim]{source}[/dim]")
        mbid = job.mbid if (job is not None and job.kind == "album") else None
        outcome = import_path(
            config,
            source,
            interactive=interactive,
            singletons=singletons,
            mbid=mbid,
        )
        if outcome.state == "imported":
            if job is not None:
                store.update_state(job.id, "imported", error=None)
            console.print(f"[green]imported[/green] {source.name}")
        elif outcome.state == "quarantined":
            if job is not None:
                store.update_state(job.id, "quarantined", error=outcome.detail)
            console.print(f"[magenta]quarantined[/magenta] {source.name}")
        else:
            if job is not None:
                store.update_state(job.id, "failed", error=outcome.detail)
            console.print(f"[red]failed[/red] {source.name}: {outcome.detail}")

    for job in quarantined:
        quarantine_dir = (
            config.paths.state / "quarantine" / Path(job.folder or "").name
        )
        if not quarantine_dir.exists():
            store.update_state(job.id, "imported", error=None)
            console.print(
                f"[green]imported[/green] {job.query} [dim](quarantine already consumed)[/dim]"
            )
            continue
        console.print(f"retrying quarantined {job.id[:8]} [dim]{quarantine_dir}[/dim]")
        outcome = import_path(
            config,
            quarantine_dir,
            interactive=interactive,
            singletons=job.kind == "track",
            mbid=job.mbid if job.kind == "album" else None,
        )
        if outcome.state == "imported":
            store.update_state(job.id, "imported", error=None)
            shutil.rmtree(quarantine_dir, ignore_errors=True)
            console.print(f"[green]imported[/green] {job.query}")
        else:
            console.print(f"[magenta]still quarantined[/magenta] {job.query}")


@lib_app.command("adopt")
def lib_adopt(
    autotag: bool = typer.Option(
        False, "--autotag", help="Autotag existing files instead of as-is import."
    ),
    no_incremental: bool = typer.Option(
        False, "--no-incremental", help="Re-scan everything, not just new files."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show plan without writing."),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Delete and regenerate the beets DB from ~/Music (applies exclusions).",
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Skip the confirmation prompt for --rebuild."
    ),
) -> None:
    """Register an existing library (~/Music) with beets, in place."""
    from .beetsbridge import adopt_library
    from .config import default_library_db, excluded_ignore_globs

    config = load_config()

    if rebuild and not dry_run:
        db = default_library_db()
        excludes = config.paths.exclude
        console.print(f"[yellow]rebuild[/yellow] will delete and regenerate {db}")
        console.print(f"a backup will be saved to {db.with_suffix(db.suffix + '.bak')}")
        if excludes:
            console.print("excluding: " + ", ".join(excludes))
        elif excluded_ignore_globs(config):
            console.print("excluding: " + ", ".join(excluded_ignore_globs(config)))
        if not yes:
            if not sys.stdout.isatty():
                console.print(
                    "[red]--rebuild needs confirmation; pass --yes in non-interactive mode[/red]"
                )
                raise SystemExit(1)
            if not confirm("proceed with rebuild?", default=False):
                console.print("aborted")
                return

    if dry_run:
        console.print(
            f"[dim]would import {config.paths.music} "
            f"({'autotag' if autotag else 'as-is'}, in place)…[/dim]"
        )
    adopt_library(
        config,
        autotag=autotag,
        incremental=not no_incremental,
        dry_run=dry_run,
        rebuild=rebuild,
    )
    console.print("[green]adopt complete[/green]")


@lib_app.command("stats")
def lib_stats() -> None:
    """Show library statistics."""
    from .beetsbridge import stats

    console.print(stats(load_config()).strip())


@lib_app.command("dupes")
def lib_dupes() -> None:
    """List duplicate tracks in the library."""
    from .beetsbridge import duplicates

    output = duplicates(load_config()).strip()
    console.print(output or "no duplicates found")


@lib_app.command("verify")
def lib_verify() -> None:
    """Check that every library item still exists on disk."""
    from .beetsbridge import verify

    total, missing = verify(load_config())
    if not missing:
        console.print(f"[green]{total} items OK[/green]")
        return
    console.print(f"[red]{len(missing)}/{total} items missing on disk:[/red]")
    for path in missing[:50]:
        console.print(f"  {path}")
    if len(missing) > 50:
        console.print(f"  … and {len(missing) - 50} more")
    raise SystemExit(1)


@lib_app.command("art")
def lib_art(
    query: list[str] = typer.Argument(None, help="Optional beets query to limit scope."),
) -> None:
    """Fetch missing album art."""
    from .beetsbridge import fetch_art

    fetch_art(load_config(), query or [])


@lib_app.command(
    "beet",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def lib_beet(ctx: typer.Context) -> None:
    """Run an arbitrary beet command against the library."""
    from .beetsbridge import passthrough

    raise SystemExit(passthrough(load_config(), ctx.args))


def _choose_release(
    query: str, mbid: str | None, config, auto: bool, quiet: bool = False
) -> Release | None:
    # In quiet mode (JSON paths) keep stdout clean: status + notices go to stderr.
    status_console = _err_console() if quiet else console
    if mbid:
        return get_release(mbid, contact=config.musicbrainz_contact)
    with status_console.status("searching MusicBrainz..."):
        summaries = search_releases(query, contact=config.musicbrainz_contact)
    if not summaries:
        status_console.print("[yellow]no MusicBrainz releases matched[/yellow]")
        return None
    if auto:
        chosen = summaries[0]
    else:
        table = Table(title="MusicBrainz releases")
        table.add_column("#", justify="right")
        table.add_column("Artist")
        table.add_column("Title")
        table.add_column("Date")
        table.add_column("Type")
        table.add_column("Tracks", justify="right")
        for index, summary in enumerate(summaries):
            table.add_row(
                str(index),
                summary.artist,
                summary.title,
                summary.date,
                summary.primary_type,
                str(summary.track_count or "?"),
            )
        console.print(table)
        index = pick_index(len(summaries), allow_refresh=False)
        if index is None:
            return None
        chosen = summaries[index]
    status_console.print(f"release: [bold]{chosen.artist} — {chosen.title}[/bold]")
    return get_release(chosen.mbid, contact=config.musicbrainz_contact)


def _pick_candidate(candidates, chain: list[str], expected: int | None, auto: bool):
    while True:
        if auto:
            return candidates[0] if candidates else None
        print_candidate_table(candidates, chain=chain, expected=expected)
        choice = pick_index(len(candidates))
        if choice is None:
            return None
        if choice == -1:
            continue
        return candidates[choice]


def _format_gate(candidate, chain: list[str], auto: bool) -> bool:
    if meets_format_preference(candidate, chain):
        return True
    available = sorted({f.extension for f in candidate.files})
    console.print(
        f"[yellow]preferred formats {chain} unavailable; "
        f"this source offers: {', '.join(available)}[/yellow]"
    )
    if auto:
        console.print("[dim]auto mode: accepting fallback format[/dim]")
        return True
    return confirm("download this format anyway?", default=False)


def _find_by_id(candidates: list[ScoredCandidate], result_id: str) -> ScoredCandidate | None:
    return next((c for c in candidates if c.id == result_id), None)


def _job_dict(job, chosen: ScoredCandidate, remote_dir: str) -> dict:
    return {
        "job_id": job.id,
        "state": job.state,
        "username": chosen.username,
        "file_count": len(chosen.files),
        "remote_dir": remote_dir,
    }


def _wait_for_job(config, store, job) -> None:
    runner = Runner(config, store)
    with Progress() as progress:
        task = progress.add_task(job.query[:40], total=1)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, total=total or 1, completed=done)

        runner.run_job(store.get(job.id), progress=on_progress)
    final = store.get(job.id)
    if final.state == "downloaded":
        console.print(f"[green]downloaded[/green] → {final.folder}")
        console.print(f"import with: [bold]music-fetcher import {job.id[:8]}[/bold]")
    else:
        console.print(f"[red]{final.state}:[/red] {final.error}")


@dl_app.command("album")
def dl_album(
    query: str = typer.Argument(None, help='e.g. "i hate myself 10 songs"'),
    artist: str = typer.Option(None, "--artist", "-a", help="Artist name (precise)."),
    album: str = typer.Option(None, "--album", "-b", help="Album title (precise)."),
    mbid: str | None = typer.Option(None, help="MusicBrainz release ID, skips lookup."),
    result_id: str | None = typer.Option(
        None, "--result-id", help="Download a specific source by id (from --list)."
    ),
    list_sources: bool = typer.Option(
        False, "--list", help="Print Soulseek candidates as JSON without downloading."
    ),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive, auto-pick best."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Queue without downloading now."),
) -> None:
    """Resolve an album on MusicBrainz, find it on Soulseek, download it.

    MusicBrainz is authoritative: the release is resolved first, then the
    Soulseek search runs on the canonical ``artist title``. Use ``--list`` to
    get JSON candidates (each with an ``id``), then re-run with ``--result-id``.
    """
    if not query and not mbid and not (artist and album):
        console.print("[red]need a query, --artist/--album, or --mbid[/red]")
        raise SystemExit(1)
    config = load_config()
    ensure_dirs(config)
    if artist and album:
        mb_query = f'artist:"{artist}" AND release:"{album}"'
    else:
        mb_query = query or ""
    # --list / --result-id are non-interactive discovery paths: auto-pick the MB
    # release and keep stdout pure JSON (status goes to stderr).
    json_mode = list_sources or result_id is not None
    auto_mb = yes or json_mode
    release = _choose_release(mb_query, mbid, config, auto=auto_mb, quiet=json_mode)
    if release is None:
        return

    chain = config.download.format_preference
    search_text = f"{release.artist} {release.title}"
    candidates = _search_candidates(
        config,
        search_text,
        artist=release.artist,
        album=release.title,
        expected_tracks=release.track_count,
    )

    if list_sources:
        _emit_json(
            {
                "release": {
                    "mbid": release.mbid,
                    "artist": release.artist,
                    "title": release.title,
                    "track_count": release.track_count,
                },
                "count": len(candidates),
                "results": [c.to_dict(chain) for c in candidates],
            }
        )
        return

    if not candidates:
        console.print("[yellow]no usable sources found[/yellow]")
        return

    if result_id is not None:
        chosen = _find_by_id(candidates, result_id)
        if chosen is None:
            _emit_json({"error": "result_id_not_found", "id": result_id})
            raise SystemExit(1)
    else:
        chosen = _pick_candidate(candidates, chain, release.track_count, auto=yes)
        if chosen is None:
            return
        if not _format_gate(chosen, chain, auto=yes):
            console.print("aborted")
            return

    files = chosen.files
    remote_dir = common_remote_dir(files)
    store = JobStore(config.paths.state / "jobs.db")
    job = store.create(
        kind="album",
        query=f"{release.artist} - {release.title}",
        mbid=release.mbid,
        expected_files=len(files),
        payload={
            "username": chosen.username,
            "remote_dir": remote_dir,
            "files": [{"filename": f.filename, "size": f.size} for f in files],
            "expected_tracks": release.track_count,
            "tracklist": [t.title for t in release.tracks],
        },
    )
    if result_id is not None:
        _emit_json(_job_dict(job, chosen, remote_dir))
    else:
        console.print(f"queued job [bold]{job.id[:8]}[/bold] from {chosen.username}")
    if no_wait:
        return
    _wait_for_job(config, store, job)


@dl_app.command("track")
def dl_track(
    query: str = typer.Argument(..., help="Search text for a single track."),
    result_id: str | None = typer.Option(
        None, "--result-id", help="Download a specific source by id (from --list)."
    ),
    list_sources: bool = typer.Option(
        False, "--list", help="Print Soulseek candidates as JSON without downloading."
    ),
    yes: bool = typer.Option(False, "--yes", help="Auto-pick best file."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Queue without downloading now."),
) -> None:
    """Search and download a single track.

    Use ``--list`` to get JSON candidates (each with an ``id``), then re-run with
    ``--result-id`` to download a specific source non-interactively.
    """
    config = load_config()
    ensure_dirs(config)
    chain = config.download.format_preference
    candidates = _search_candidates(config, query, expected_tracks=1)

    if list_sources:
        _emit_json(
            {
                "query": query,
                "count": len(candidates),
                "results": [c.to_dict(chain) for c in candidates],
            }
        )
        return

    if not candidates:
        console.print("[yellow]no usable sources found[/yellow]")
        return

    if result_id is not None:
        chosen = _find_by_id(candidates, result_id)
        if chosen is None:
            _emit_json({"error": "result_id_not_found", "id": result_id})
            raise SystemExit(1)
    else:
        chosen = _pick_candidate(candidates, chain, None, auto=yes)
        if chosen is None:
            return
        if not _format_gate(chosen, chain, auto=yes):
            console.print("aborted")
            return

    files = chosen.files
    if result_id is not None or yes or len(files) == 1:
        selected_files = [best_single_file(files, chain)]
    else:
        table = Table(title=f"files from {chosen.username}")
        table.add_column("#", justify="right")
        table.add_column("File")
        table.add_column("Size", justify="right")
        for index, file in enumerate(files):
            name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
            table.add_row(str(index), name, format_size(file.size))
        console.print(table)
        index = pick_index(len(files), allow_refresh=False)
        if index is None:
            return
        selected_files = [files[index]]

    remote_dir = common_remote_dir(selected_files)
    store = JobStore(config.paths.state / "jobs.db")
    job = store.create(
        kind="track",
        query=query,
        expected_files=len(selected_files),
        payload={
            "username": chosen.username,
            "remote_dir": remote_dir,
            "filenames": [f.filename for f in selected_files],
            "files": [{"filename": f.filename, "size": f.size} for f in selected_files],
        },
    )
    if result_id is not None:
        _emit_json(
            {
                "job_id": job.id,
                "state": job.state,
                "username": chosen.username,
                "file_count": len(selected_files),
                "remote_dir": remote_dir,
            }
        )
    else:
        console.print(f"queued job [bold]{job.id[:8]}[/bold] from {chosen.username}")
    if no_wait:
        return
    _wait_for_job(config, store, job)


@app.command()
def version() -> None:
    console.print(f"music-fetcher {__version__}")


def main() -> None:
    try:
        app()
    except SlskdError as error:
        console.print(f"[red]slskd error:[/red] {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
