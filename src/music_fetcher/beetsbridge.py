from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import APP_NAME, AppConfig
from .doctor import resolve_beet


class BeetsError(RuntimeError):
    pass


def config_path(config: AppConfig) -> Path:
    return config.paths.state / "config.yaml"


def generate_config(config: AppConfig) -> Path:
    config.paths.state.mkdir(parents=True, exist_ok=True)
    plugins = config.beets.plugins or []
    doc = {
        "plugins": plugins,
        "import": {
            "copy": False,
            "move": config.beets.move_imported,
            "write": True,
            "quiet_fallback": config.beets.quiet_fallback,
            "duplicate_action": "skip",
            "languages": "en",
        },
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
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    beet = resolve_beet()
    if beet is None:
        raise BeetsError("beet binary not found")
    base = generate_config(config)
    command = [beet, "-c", str(base)]
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
) -> ImportOutcome:
    generate_config(config)
    args = ["import"]
    if not interactive:
        args.append("--quiet")
    if singletons:
        args.append("--singletons")
    extra = None
    if singletons and not interactive:
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


def adopt_library(
    config: AppConfig,
    *,
    autotag: bool = False,
    incremental: bool = True,
    dry_run: bool = False,
) -> str:
    generate_config(config)
    args = ["import"]
    if not autotag:
        args.append("-A")
    if incremental:
        args.append("--incremental")
    if dry_run:
        args.append("--pretend")
    args.extend(
        [
            "--set",
            "import.copy=no",
            "--set",
            "import.move=no",
            "--set",
            "import.write=no",
            "--set",
            "import.duplicate_action=skip",
            str(config.paths.music),
        ]
    )
    result = run_beet(config, args, capture=False)
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

