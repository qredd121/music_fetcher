from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "music-fetcher"

DEFAULT_CONFIG_PATH = Path("~/.config/music-fetcher/config.toml").expanduser()


@dataclass
class SlskdConfig:
    base_url: str = "http://localhost:5030"
    api_prefix: str = "/api/v0"
    api_key: str = ""
    timeout: float = 30.0


@dataclass
class PathsConfig:
    downloads: Path = field(
        default_factory=lambda: Path("~/.local/state/music-fetcher/downloads").expanduser()
    )
    music: Path = field(default_factory=lambda: Path("~/Music").expanduser())
    state: Path = field(
        default_factory=lambda: Path("~/.local/state/music-fetcher").expanduser()
    )
    # Directories relative to `music` that are ignored by all library operations.
    exclude: list[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    timeout: float = 45.0
    poll_interval: float = 1.5


@dataclass
class DownloadConfig:
    format_preference: list[str] = field(
        default_factory=lambda: ["FLAC", "MP3 320", "MP3 V0"]
    )
    timeout: float = 1800.0
    retries: int = 2
    stall_seconds: float = 300.0
    queue_wait_seconds: float = 600.0


@dataclass
class BeetsConfig:
    plugins: list[str] = field(default_factory=lambda: ["fetchart", "duplicates"])
    quiet_fallback: str = "skip"
    move_imported: bool = True


@dataclass
class AppConfig:
    slskd: SlskdConfig
    paths: PathsConfig
    search: SearchConfig
    download: DownloadConfig
    beets: BeetsConfig
    musicbrainz_contact: str = ""
    log_level: str = "INFO"


def _parse_formats(value: str) -> list[str]:
    return [part.strip().upper() for part in value.split(",") if part.strip()]


_ENV_OVERRIDES: dict[tuple[str, str], tuple[str, object]] = {
    ("slskd", "base_url"): ("SLSKD_BASE_URL", str),
    ("paths", "downloads"): ("DOWNLOADS_DIR", lambda v: Path(v).expanduser()),
    ("paths", "music"): ("MUSIC_DIR", lambda v: Path(v).expanduser()),
    ("paths", "state"): ("STATE_DIR", lambda v: Path(v).expanduser()),
    ("search", "timeout"): ("SEARCH_TIMEOUT", float),
    ("download", "timeout"): ("DOWNLOAD_TIMEOUT", float),
    ("download", "format_preference"): ("PREFER_FORMAT", _parse_formats),
}

_TOP_LEVEL_ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "musicbrainz_contact": ("MUSICBRAINZ_CONTACT", str),
    "log_level": ("LOG_LEVEL", str),
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_config(config_path: Path | None = None) -> AppConfig:
    load_dotenv()

    defaults = {
        "slskd": {},
        "paths": {},
        "search": {},
        "download": {},
        "beets": {},
        "musicbrainz_contact": "",
        "log_level": "INFO",
    }
    file_data = _load_toml(config_path or DEFAULT_CONFIG_PATH)
    data = _deep_merge(defaults, file_data)

    for (section, key), (env_name, cast) in _ENV_OVERRIDES.items():
        if os.environ.get(env_name) is not None:
            data.setdefault(section, {})[key] = cast(os.environ[env_name])
    for key, (env_name, cast) in _TOP_LEVEL_ENV_OVERRIDES.items():
        if os.environ.get(env_name) is not None:
            data[key] = cast(os.environ[env_name])

    config = AppConfig(
        slskd=_build(SlskdConfig, data["slskd"]),
        paths=_build(PathsConfig, data["paths"]),
        search=_build(SearchConfig, data["search"]),
        download=_build(DownloadConfig, data["download"]),
        beets=_build(BeetsConfig, data["beets"]),
        musicbrainz_contact=data.get("musicbrainz_contact", ""),
        log_level=str(data.get("log_level", "INFO")).upper(),
    )
    config.slskd.api_key = os.environ.get("SLSKD_API_KEY", "")
    return config


def ensure_dirs(config: AppConfig) -> None:
    config.paths.state.mkdir(parents=True, exist_ok=True)
    config.paths.downloads.mkdir(parents=True, exist_ok=True)
    config.paths.music.mkdir(parents=True, exist_ok=True)


def excluded_ignore_globs(config: AppConfig) -> list[str]:
    """Glob patterns for beets' ``ignore`` derived from ``paths.exclude``.

    beets applies ``ignore`` patterns with ``fnmatch`` against a single path
    component (the directory/file *basename*) as it walks the tree, so patterns
    must be the leaf segment of the excluded path. For ``I/Incelcore`` this
    yields ``Incelcore``, which makes beets skip that directory during import.
    """
    globs: list[str] = []
    for entry in config.paths.exclude:
        rel = entry.strip().strip("/").replace("\\", "/")
        if not rel:
            continue
        leaf = rel.rsplit("/", 1)[-1]
        if leaf and leaf not in globs:
            globs.append(leaf)
    return globs


def default_library_db() -> Path:
    """Resolve beets' default library database path.

    music-fetcher does not pin ``library`` in its generated config, so beets
    uses its default location. Honor ``BEETSDIR`` like beets does, otherwise
    fall back to ``~/.config/beets/library.db``.
    """
    beetsdir = os.environ.get("BEETSDIR")
    base = Path(beetsdir).expanduser() if beetsdir else Path("~/.config/beets").expanduser()
    return base / "library.db"
