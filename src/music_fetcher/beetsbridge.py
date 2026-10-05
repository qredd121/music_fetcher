from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import (
    AppConfig,
    default_library_db,
    excluded_ignore_globs,
)
from .doctor import resolve_beet
from .tags import audio_files


class BeetsError(RuntimeError):
    pass


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

_DEFAULT_IGNORE = [
    ".*",
    "*~",
    "System Volume Information",
    "lost+found",
]


def config_path(config: AppConfig) -> Path:
    return config.paths.state / "config.yaml"


def _deep_merge(base: dict, overrides: dict) -> dict:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def generate_config(config: AppConfig, overrides: dict | None = None) -> Path:
    config.paths.state.mkdir(parents=True, exist_ok=True)
    plugins = list(config.beets.plugins or [])
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
            "duplicate_action": "skip",
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
    if overrides:
        _deep_merge(doc, overrides)
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
    base = generate_config(config, overrides=extra_config)
    command = [beet, "-c", str(base)]
    if plugins is not None:
        command.extend(["-p", plugins])
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


def _has_audio(path: Path) -> bool:
    return bool(audio_files(path))


def import_path(
    config: AppConfig,
    source: Path,
    *,
    singletons: bool = False,
) -> ImportOutcome:
    args = ["import", "-A", "--quiet"]
    if singletons:
        args.append("--singletons")
    args.append(str(source))
    try:
        result = run_beet(config, args)
    except BeetsError as error:
        return ImportOutcome(state="failed", detail=str(error))
    if not _has_audio(source):
        return ImportOutcome(state="imported", detail=result.stdout.strip()[-200:])
    return ImportOutcome(state="skipped", detail="beets left files behind (duplicate?)")


def backup_library_db() -> Path | None:
    db = default_library_db()
    if not db.exists():
        return None
    backup = db.with_suffix(db.suffix + ".bak")
    shutil.copy2(db, backup)
    return backup


def adopt_library(
    config: AppConfig,
    *,
    incremental: bool = True,
    dry_run: bool = False,
    rebuild: bool = False,
) -> str:
    generate_config(config)

    if rebuild:
        backup_library_db()
        db = default_library_db()
        if db.exists():
            db.unlink()
        incremental = False

    args = ["import", "-A", "-C", "-M", "-W"]
    if incremental:
        args.append("--incremental")
    if dry_run:
        args.append("--pretend")
    args.append(str(config.paths.music))
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

