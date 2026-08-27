from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from .selection import (
    ScoredCandidate,
    audio_files,
    average_bitrate,
    best_format,
    format_rank,
)
from .slskd import SearchResult

console = Console()


def _fmt_size(num_bytes: int) -> str:
    size_mb = num_bytes / (1024 * 1024)
    if size_mb >= 1024:
        return f"{size_mb / 1024:.2f} GB"
    return f"{size_mb:.1f} MB"


def format_size(num_bytes: int) -> str:
    return _fmt_size(num_bytes)


def _fmt_speed(speed: int) -> str:
    return f"{speed // 1024} KB/s"


def print_search_table(
    results: list[SearchResult],
    *,
    chain: list[str] | None = None,
    expected: int | None = None,
) -> None:
    table = Table(title="Search results", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("User")
    table.add_column("Folder")
    table.add_column("Format")
    if expected:
        table.add_column("Tracks", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Slot")
    table.add_column("Bitrate", justify="right")
    table.add_column("Speed")

    for index, result in enumerate(results):
        slot = "[green]free[/green]" if result.has_free_slot else "[red]queued[/red]"
        if chain:
            formats = best_format(result, chain)
            rank = format_rank(result, chain)
            style = "green" if rank < len(chain) else "yellow"
            formats = f"[{style}]{formats}[/{style}]"
        else:
            formats = (
                "/".join(sorted({f.extension for f in audio_files(result)})) or "?"
            )
        row = [
            str(index),
            result.username[:18],
            result.folder[:38],
            formats,
        ]
        if expected:
            count = len(audio_files(result))
            match = (
                "[green]✓[/green]"
                if count == expected
                else f"[yellow]{count}[/yellow]"
            )
            row.append(f"{match}/{expected}")
        bitrate = average_bitrate(result) or "-"
        row.extend(
            [
                str(len(result.files)),
                _fmt_size(result.total_size),
                slot,
                str(bitrate),
                _fmt_speed(result.upload_speed),
            ]
        )
        table.add_row(*row)
    console.print(table)


def pick_index(count: int, allow_refresh: bool = True) -> int | None:
    choices = "number=select r=refresh q=quit" if allow_refresh else "number=select q=quit"
    console.print(f"[dim]{choices}[/dim]")
    while True:
        raw = console.input("choice> ").strip().lower()
        if raw == "q":
            return None
        if raw == "r" and allow_refresh:
            return -1
        if raw.isdigit() and int(raw) < count:
            return int(raw)
        console.print("[red]invalid choice[/red]")


def confirm(question: str, default: bool = False) -> bool:
    return Confirm.ask(question, default=default)


def print_candidate_table(
    candidates: list[ScoredCandidate],
    *,
    chain: list[str],
    expected: int | None = None,
) -> None:
    table = Table(title="Sources", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("User")
    table.add_column("Folder")
    table.add_column("Format")
    if expected:
        table.add_column("Tracks", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Slot")
    table.add_column("Bitrate", justify="right")

    for index, cand in enumerate(candidates):
        style = "green" if cand.format_rank < len(chain) else "yellow"
        formats = "/".join(sorted({f.extension for f in cand.files}))
        slot = "[green]free[/green]" if cand.has_free_slot else "[red]queued[/red]"
        row = [
            str(index),
            cand.username[:18],
            cand.folder_name[:40],
            f"[{style}]{formats}[/{style}]",
        ]
        if expected:
            count = len(cand.files)
            match = "[green]✓[/green]" if count == expected else f"[yellow]{count}[/yellow]"
            row.append(f"{match}/{expected}")
        row.extend(
            [
                str(len(cand.files)),
                _fmt_size(cand.total_size),
                slot,
                str(cand.bitrate or "-"),
            ]
        )
        table.add_row(*row)
    console.print(table)
