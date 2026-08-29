from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import (
    APP_NAME,
    AppConfig,
    default_library_db,
    excluded_ignore_globs,
)
from .doctor import resolve_beet


class BeetsError(RuntimeError):
    pass


# Inline field that maps an artist name to its first-letter bucket, with a
# ``#`` fallback for empty or non-ASCII names (transliterated via unidecode
# when available). Without this, an empty/non-ASCII albumartist produces an
# empty bucket and files land directly in the Music root.
_ARTIST_LETTER_FIELD = (
    "\nfirst = (albumartist or artist or '#').strip()\n"
    "if not first:\n"
    "    return '#'\n"
    "ch = first[0]\n"
    "if ch.isascii() and ch.isalpha():\n"
    "    return ch.upper()\n"
    "try:\n"
    "    from unidecode import unidecode\n"
    "    decoded = unidecode(first).strip()\n"
    "    if decoded and decoded[0].isascii() and decoded[0].isalpha():\n"
    "        return decoded[0].upper()\n"
    "except ImportError:\n"
    "    pass\n"
    "return '#'\n"
)

# beets' built-in default ignore globs; we extend rather than replace these.
_DEFAULT_IGNORE = [
    ".*",
    "*~",
    "System Volume Information",
    "lost+found",
]


def config_path(config: AppConfig) -> Path:
    return config.paths.state / "config.yaml"


def generate_config(config: AppConfig) -> Path:
    config.paths.state.mkdir(parents=True, exist_ok=True)
    plugins = list(config.beets.plugins or [])
    # `inline` powers the $artist_letter field used in the path templates.
    if "inline" not in plugins:
        plugins.insert(0, "inline")
    ignore = list(_DEFAULT_IGNORE) + excluded_ignore_globs(config)
    doc = {
        "directory": str(config.paths.music),
        "plugins": plugins,
        "ignore": ignore,
        "import": {
            "copy": False,
            "move": config.beets.move_imported,
            "write": True,
            "quiet_fallback": config.beets.quiet_fallback,
            "duplicate_action": "skip",
            "languages": "en",
        },
        "paths": {
            "default": "$artist_letter/$albumartist/$year - $album%aunique{}/$track - $title",
            "singleton": "$artist_letter/$artist/$title",
            "comp": "$artist_letter/$albumartist/$year - $album%aunique{}/$track - $title",
        },
        "item_fields": {"artist_letter": _ARTIST_LETTER_FIELD},
        "album_fields": {"artist_letter": _ARTIST_LETTER_FIELD},
        "fetchart": {
            "auto": True,
            "minwidth": 300,
        },
    }
    path = config_path(config)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def run_beet(
    config: AppConfig,
    args: list[str],
    *,
    extra_config: dict | None = None,
    plugins: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    beet = resolve_beet()
    if beet is None:
        raise BeetsError("beet binary not found")
    base = generate_config(config)
    command = [beet, "-c", str(base)]
    # A second `-c` overlay causes confuse to resolve list options (notably
    # `ignore`) from higher-priority sources, shadowing the base config's
    # values. Prefer the global `-p` flag for plugin overrides so exclusions
    # survive.
    if plugins is not None:
        command.extend(["-p", plugins])
    if extra_config:
        override_dir = Path(
            os.environ.get("MF_TMPDIR", os.environ.get("TMPDIR", "/tmp"))
        )
        override_dir.mkdir(parents=True, exist_ok=True)
        override = override_dir / f"{APP_NAME}-beets-override.yaml"
        override.write_text(yaml.safe_dump(extra_config, sort_keys=False))
        command.extend(["-c", str(override)])
    command.extend(args)
    result = subprocess.run(
        command,
        capture_output=capture,
        text=True,
        check=False,
    )
    if capture and result.returncode != 0 and result.stderr:
        raise BeetsError(result.stderr.strip()[:300])
    return result


@dataclass
class ImportOutcome:
    state: str
    detail: str


AUDIO_SUFFIXES = {
    ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".aiff", ".aif",
    ".wma", ".wv", ".ape", ".alac",
}


def _has_audio(path: Path) -> bool:
    return any(item.suffix.lower() in AUDIO_SUFFIXES for item in path.rglob("*"))


def import_path(
    config: AppConfig,
    source: Path,
    *,
    interactive: bool = False,
    singletons: bool = False,
    mbid: str | None = None,
) -> ImportOutcome:
    generate_config(config)
    args = ["import"]
    if not interactive:
        args.append("--quiet")
    if singletons:
        args.append("--singletons")
    if mbid:
        args.extend(["--search-id", mbid])
    extra = None
    if not interactive and (singletons or mbid):
        extra = {"import": {"quiet_fallback": "asis"}}
    args.append(str(source))
    try:
        result = run_beet(config, args, extra_config=extra)
    except BeetsError as error:
        return ImportOutcome(state="failed", detail=str(error))

    if not _has_audio(source):
        return ImportOutcome(state="imported", detail=result.stdout.strip()[-200:])

    quarantine = config.paths.state / "quarantine" / source.name
    if source.resolve() == quarantine.resolve():
        return ImportOutcome(
            state="failed",
            detail="autotag did not consume the files",
        )
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    if quarantine.exists():
        shutil.rmtree(quarantine)
    shutil.move(str(source), str(quarantine))
    return ImportOutcome(
        state="quarantined",
        detail=f"autotag did not complete; moved to {quarantine}",
    )


def backup_library_db() -> Path | None:
    """Copy beets' default library DB to ``<name>.bak`` and return the backup path."""
    db = default_library_db()
    if not db.exists():
        return None
    backup = db.with_suffix(db.suffix + ".bak")
    shutil.copy2(db, backup)
    return backup


def adopt_library(
    config: AppConfig,
    *,
    autotag: bool = False,
    incremental: bool = True,
    dry_run: bool = False,
    rebuild: bool = False,
) -> str:
    generate_config(config)

    if rebuild:
        # Regenerate from scratch: back up and delete the DB, then do a full
        # (non-incremental) in-place import so exclusions are re-applied.
        backup_library_db()
        db = default_library_db()
        if db.exists():
            db.unlink()
        incremental = False

    args = ["import"]
    if not autotag:
        args.append("-A")
    # Force a strictly in-place adopt: never copy, move, or rewrite tags. These
    # must be real CLI flags -- `--set import.move=no` would be interpreted by
    # beets as setting an item metadata field, not a config value.
    args.extend(["-C", "-M", "-W"])
    if incremental:
        args.append("--incremental")
    if dry_run:
        args.append("--pretend")
    args.append(str(config.paths.music))
    # Disable plugins via the global `-p ''` flag (not a second `-c` overlay),
    # so the base config's `ignore` exclusions are preserved. `duplicate_action`
    # is already `skip` in the generated base config.
    result = run_beet(config, args, plugins="", capture=False)
    return str(result.returncode)


def stats(config: AppConfig) -> str:
    return run_beet(config, ["stats"]).stdout


def duplicates(config: AppConfig) -> str:
    return run_beet(
        config,
        ["duplicates", "-f", "$albumartist - $album - $title [$format] ($path)"],
    ).stdout


def fetch_art(config: AppConfig, query: list[str]) -> int:
    args = ["fetchart"]
    args.extend(query)
    return run_beet(config, args, capture=False).returncode


def passthrough(config: AppConfig, args: list[str]) -> int:
    return run_beet(config, args, capture=False).returncode


def verify(config: AppConfig) -> tuple[int, list[str]]:
    output = run_beet(config, ["ls", "-f", "$path"]).stdout
    paths = [line for line in output.splitlines() if line.strip()]
    missing = [path for path in paths if not Path(path).exists()]
    return len(paths), missing

