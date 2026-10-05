from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn

import typer
from rich.progress import Progress
from rich.prompt import Confirm
from rich.table import Table

from . import __version__, daemon
from . import doctor as doctor_mod
from .config import ensure_dirs, load_config
from .importer import ImportResult, import_source, in_quarantine, quarantine_root
from .jobs import ACTIVE_STATES, Runner
from .jobs.db import JobStore
from .selection import (
    ScoredCandidate,
    best_single_file,
    common_remote_dir,
    meets_format_preference,
    score_candidates,
)
from .slskd import SlskdClient, SlskdError
from .tags import merge_meta, read_meta, resolve, write_meta, write_plan
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
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _err_console():
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
    """Check slskd, paths and beets."""
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


tags_app = typer.Typer(
    help="Inspect and fix tags of staged or quarantined folders.", no_args_is_help=True
)
app.add_typer(tags_app, name="tags")


def _fail(message: str, error: str, **extra) -> NoReturn:
    _err_console().print(f"[red]{message}[/red]")
    _emit_json({"error": error, **extra})
    raise SystemExit(1)


def _is_single(job) -> bool:
    return job is not None and job.kind == "track"


def _job_meta(job) -> dict | None:
    return job.payload.get("meta") if job is not None else None


def _job_path(config, job) -> Path | None:
    if not job.folder:
        return None
    path = Path(job.folder)
    if path.exists():
        return path
    fallback = quarantine_root(config) / path.name
    return fallback if fallback.exists() else None


def _job_for_path(config, store, path: Path):
    target = path.resolve()
    for job in store.list():
        job_path = _job_path(config, job)
        if job_path is not None and job_path.resolve() == target:
            return job
    return None


def _resolve_target(config, store, target: str):
    for candidate in (Path(target).expanduser(), quarantine_root(config) / target):
        if candidate.is_dir():
            return _job_for_path(config, store, candidate), candidate
    matches = [job for job in store.list() if job.id.startswith(target)]
    if len(matches) != 1:
        _fail(f"no folder or unique job matching {target}", "target_not_found", target=target)
    job = matches[0]
    path = _job_path(config, job)
    if path is None:
        _fail(f"job {job.id[:8]} has no folder on disk", "folder_missing", job_id=job.id)
    return job, path


def _record(store, job, result: ImportResult) -> None:
    if job is None:
        return
    if result.state == "imported":
        store.update_state(job.id, "imported", error="")
        return
    fields = {"folder": str(result.source)} if result.state == "quarantined" else None
    store.update_state(job.id, result.state, error=result.detail, set_fields=fields)


_RESULT_STYLES = {"imported": "green", "quarantined": "magenta", "failed": "red"}


def _print_result(out, label: str, result: ImportResult) -> None:
    style = _RESULT_STYLES.get(result.state, "yellow")
    if result.state == "imported":
        out.print(f"[{style}]imported[/{style}] {label}")
        return
    out.print(f"[{style}]{result.state}[/{style}] {label}: {result.detail}")
    if result.plan and result.plan["missing"]:
        for name, fields in result.plan["missing"].items():
            out.print(f"  [dim]{name}[/dim] missing {', '.join(fields)}")
    if result.state == "quarantined":
        out.print(f"  fix with: music-fetcher tags show {result.source.name!r}")


@app.command("import")
def import_(
    target: str = typer.Argument(
        None, help="Job prefix, folder path or quarantine folder name."
    ),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive; JSON on stdout."),
    staged: bool = typer.Option(
        False, "--staged", help="Also import orphaned folders in the downloads dir."
    ),
) -> None:
    """Check tags of staged downloads and file them into the library.

    Folders with missing or inconsistent tags are quarantined; inspect them
    with ``tags show`` and fix them with ``tags set``.
    """
    config = load_config()
    store = JobStore(config.paths.state / "jobs.db")
    out = _err_console() if yes else console
    sources: list[tuple[object, Path]] = []

    if target:
        sources.append(_resolve_target(config, store, target))
    else:
        for job in store.list(states=("downloaded", "quarantined")):
            path = _job_path(config, job)
            if path is not None:
                sources.append((job, path))
            elif job.state == "quarantined":
                store.update_state(job.id, "imported", error="")
                out.print(f"[green]imported[/green] {job.query} [dim](already consumed)[/dim]")
            else:
                store.update_state(job.id, "failed", error="staged folder missing")
                out.print(f"[red]{job.id[:8]} staged path missing: {job.folder}[/red]")
        known = {path.resolve() for _, path in sources}
        job_names = {Path(job.folder).name for job in store.list() if job.folder}
        roots = [quarantine_root(config)]
        if staged:
            roots.append(config.paths.downloads)
        for root in roots:
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if not entry.is_dir() or entry.resolve() in known:
                    continue
                if root == config.paths.downloads and entry.name in job_names:
                    continue
                sources.append((None, entry))

    results = []
    for job, path in sources:
        label = job.id[:8] if job is not None else path.name
        out.print(f"importing {label} [dim]{path}[/dim]")
        result = import_source(config, path, _job_meta(job), singletons=_is_single(job))
        _record(store, job, result)
        _print_result(out, label, result)
        results.append({"job_id": job.id if job is not None else None, **result.to_dict()})

    if not sources:
        out.print("nothing to import")
    if yes:
        _emit_json({"count": len(results), "results": results})


@tags_app.command("show")
def tags_show(
    target: str = typer.Argument(..., help="Job prefix, folder path or quarantine folder name."),
) -> None:
    """Print current tags, proposed tags and what is missing, as JSON."""
    config = load_config()
    store = JobStore(config.paths.state / "jobs.db")
    job, path = _resolve_target(config, store, target)
    meta = merge_meta(_job_meta(job), read_meta(path))
    plan = resolve(path, meta, singletons=_is_single(job))
    _emit_json({"job_id": job.id if job is not None else None, "meta": meta, **plan.to_dict()})


def _print_plan(out, plan) -> None:
    for entry in plan.files:
        for name, (old, new) in entry.changes.items():
            out.print(f"[dim]{entry.file}[/dim] {name}: {old!r} -> [bold]{new!r}[/bold]")
    for name, fields in plan.missing.items():
        out.print(f"[magenta]{name}[/magenta] still missing {', '.join(fields)}")
    for problem in plan.problems:
        out.print(f"[magenta]{problem}[/magenta]")


@tags_app.command("set")
def tags_set(
    target: str = typer.Argument(..., help="Job prefix, folder path or quarantine folder name."),
    album_artist: str | None = typer.Option(None, "--album-artist"),
    artist: str | None = typer.Option(None, "--artist"),
    album: str | None = typer.Option(None, "--album"),
    year: int | None = typer.Option(None, "--year"),
    file: str | None = typer.Option(
        None, "--file", help="File index (from tags show) or relative path."
    ),
    title: str | None = typer.Option(None, "--title"),
    track: int | None = typer.Option(None, "--track"),
    disc: int | None = typer.Option(None, "--disc"),
    meta_json: str | None = typer.Option(
        None, "--meta", help='JSON: {"album": {...}, "files": {"<index|path>": {...}}}.'
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, write nothing."),
    yes: bool = typer.Option(False, "--yes", help="Apply without confirmation."),
    no_import: bool = typer.Option(False, "--no-import", help="Save overrides only."),
) -> None:
    """Override tags of a staged or quarantined folder, then import it.

    Album-level options apply to every file; ``--title``/``--track``/``--disc``
    apply to the file given by ``--file``. Overrides are saved in the folder
    and survive retries.
    """
    config = load_config()
    store = JobStore(config.paths.state / "jobs.db")
    job, path = _resolve_target(config, store, target)
    singletons = _is_single(job)
    existing = read_meta(path)
    names = [e.file for e in resolve(path, merge_meta(_job_meta(job), existing)).files]

    def key_for(ref: str) -> str:
        if ref.isdigit() and 1 <= int(ref) <= len(names):
            return names[int(ref) - 1]
        if ref in names:
            return ref
        _fail(f"unknown file {ref}", "file_not_found", file=ref)

    update: dict = {"album": {}, "files": {}}
    if meta_json:
        try:
            data = json.loads(meta_json)
        except json.JSONDecodeError as error:
            _fail(f"invalid --meta: {error}", "invalid_meta")
        update["album"].update(data.get("album") or {})
        for ref, values in (data.get("files") or {}).items():
            update["files"].setdefault(key_for(str(ref)), {}).update(values or {})

    album_level = {"albumartist": album_artist, "album": album, "year": year}
    file_level = {"title": title, "track": track, "disc": disc}
    (file_level if file else album_level)["artist"] = artist
    update["album"].update({k: v for k, v in album_level.items() if v is not None})
    per_file = {k: v for k, v in file_level.items() if v is not None}
    if per_file:
        if file is None and len(names) != 1:
            _fail("--title/--track/--disc need --file", "file_required")
        ref = key_for(file) if file is not None else names[0]
        update["files"].setdefault(ref, {}).update(per_file)
    if not update["album"] and not update["files"]:
        _fail("nothing to set", "nothing_to_set")

    new_meta = merge_meta(existing, update)
    plan = resolve(path, merge_meta(_job_meta(job), new_meta), singletons=singletons)
    report = {
        "job_id": job.id if job is not None else None,
        "dry_run": dry_run,
        "meta": new_meta,
        **plan.to_dict(),
    }
    if dry_run:
        _emit_json(report)
        return
    if not yes:
        if not sys.stdin.isatty():
            _fail("confirmation required; pass --yes", "confirmation_required")
        err = _err_console()
        _print_plan(err, plan)
        if not Confirm.ask("apply these tags?", default=False, console=err):
            _fail("aborted", "aborted")

    write_meta(path, new_meta)
    if in_quarantine(config, path):
        write_plan(path, plan)
    report["import"] = None
    if plan.ok and not no_import:
        result = import_source(config, path, _job_meta(job), singletons=singletons)
        _record(store, job, result)
        _print_result(_err_console(), path.name, result)
        report["import"] = result.to_dict()
    _emit_json(report)

@lib_app.command("adopt")
def lib_adopt(
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
            "(as-is, in place)…[/dim]"
        )
    adopt_library(
        config,
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


def _choose_candidate(candidates, chain, expected, result_id, yes):
    if result_id is not None:
        chosen = _find_by_id(candidates, result_id)
        if chosen is None:
            _fail(f"no candidate with id {result_id}", "result_id_not_found", id=result_id)
        return chosen
    if not candidates:
        console.print("[yellow]no usable sources found[/yellow]")
        return None
    chosen = _pick_candidate(candidates, chain, expected, auto=yes)
    if chosen is None:
        return None
    if not _format_gate(chosen, chain, auto=yes):
        console.print("aborted")
        return None
    return chosen


@dl_app.command("album")
def dl_album(
    query: str = typer.Argument(None, help="Override the Soulseek search text."),
    artist: str = typer.Option(..., "--artist", "-a", help="Album artist, written to tags."),
    album: str = typer.Option(..., "--album", "-b", help="Album title, written to tags."),
    year: int | None = typer.Option(None, "--year", "-y", help="Release year, written to tags."),
    tracks: int | None = typer.Option(
        None, "--tracks", help="Expected track count, used for ranking."
    ),
    result_id: str | None = typer.Option(
        None, "--result-id", help="Download a specific source by id (from --list)."
    ),
    list_sources: bool = typer.Option(
        False, "--list", help="Print Soulseek candidates as JSON without downloading."
    ),
    yes: bool = typer.Option(False, "--yes", help="Non-interactive, auto-pick best."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Queue without downloading now."),
) -> None:
    """Find an album on Soulseek and download it.

    ``--artist``, ``--album`` and ``--year`` are authoritative: they are written
    to the files on import, overriding the uploader's tags. Use ``--list`` to
    get JSON candidates (each with an ``id``), then re-run with ``--result-id``.
    """
    config = load_config()
    ensure_dirs(config)
    chain = config.download.format_preference
    search_text = query or f"{artist} {album}"
    candidates = _search_candidates(
        config, search_text, artist=artist, album=album, expected_tracks=tracks
    )

    if list_sources:
        _emit_json(
            {
                "query": search_text,
                "artist": artist,
                "album": album,
                "year": year,
                "count": len(candidates),
                "results": [c.to_dict(chain) for c in candidates],
            }
        )
        return

    chosen = _choose_candidate(candidates, chain, tracks, result_id, yes)
    if chosen is None:
        return

    meta_album = {"albumartist": artist, "album": album}
    if year:
        meta_album["year"] = year
    files = chosen.files
    remote_dir = common_remote_dir(files)
    store = JobStore(config.paths.state / "jobs.db")
    job = store.create(
        kind="album",
        query=f"{artist} - {album}",
        expected_files=len(files),
        payload={
            "username": chosen.username,
            "remote_dir": remote_dir,
            "files": [{"filename": f.filename, "size": f.size} for f in files],
            "expected_tracks": tracks,
            "meta": {"album": meta_album},
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
    query: str = typer.Argument(None, help="Search text; defaults to '<artist> <title>'."),
    artist: str | None = typer.Option(
        None, "--artist", "-a", help="Track artist, written to tags."
    ),
    title: str | None = typer.Option(None, "--title", "-t", help="Track title, written to tags."),
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
    if not query and not (artist and title):
        _fail("need a query or --artist and --title", "missing_query")
    config = load_config()
    ensure_dirs(config)
    chain = config.download.format_preference
    search_text = query or f"{artist} {title}"
    terms = f"{artist or ''} {title or ''}".strip() or search_text
    candidates = _search_candidates(config, search_text, album=terms, expected_tracks=1)

    if list_sources:
        _emit_json(
            {
                "query": search_text,
                "count": len(candidates),
                "results": [c.to_dict(chain) for c in candidates],
            }
        )
        return

    chosen = _choose_candidate(candidates, chain, None, result_id, yes)
    if chosen is None:
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

    meta_track = {k: v for k, v in {"artist": artist, "title": title}.items() if v}
    remote_dir = common_remote_dir(selected_files)
    store = JobStore(config.paths.state / "jobs.db")
    job = store.create(
        kind="track",
        query=query or f"{artist} - {title}",
        expected_files=len(selected_files),
        payload={
            "username": chosen.username,
            "remote_dir": remote_dir,
            "filenames": [f.filename for f in selected_files],
            "files": [{"filename": f.filename, "size": f.size} for f in selected_files],
            "meta": {"album": meta_track},
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
