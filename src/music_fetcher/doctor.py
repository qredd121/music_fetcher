from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import APP_NAME, AppConfig
from .daemon import ComposeError, find_compose_file


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


def resolve_beet() -> str | None:
    venv_beet = Path(sys.prefix) / "bin" / "beet"
    if venv_beet.exists():
        return str(venv_beet)
    return shutil.which("beet")


def _check_api_key(config: AppConfig) -> CheckResult:
    if config.slskd.api_key:
        return CheckResult("api-key", "OK", "SLSKD_API_KEY is set")
    return CheckResult("api-key", "FAIL", "SLSKD_API_KEY missing (.env or environment)")


def _check_slskd(config: AppConfig) -> CheckResult:
    from .slskd import SlskdClient, SlskdError

    try:
        client = SlskdClient(config.slskd)
    except SlskdError as error:
        return CheckResult("slskd", "FAIL", str(error))
    if client.ping():
        return CheckResult("slskd", "OK", f"{config.slskd.base_url} reachable")
    return CheckResult("slskd", "FAIL", f"{config.slskd.base_url} not responding")


def _check_compose(config: AppConfig) -> CheckResult:
    try:
        path = find_compose_file()
    except ComposeError as error:
        return CheckResult("compose", "WARN", str(error))
    return CheckResult("compose", "OK", str(path))


def _check_dirs(config: AppConfig) -> CheckResult:
    problems = []
    for label, path in (
        ("downloads", config.paths.downloads),
        ("music", config.paths.music),
        ("state", config.paths.state),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            if not os.access(path, os.W_OK):
                problems.append(f"{label} not writable")
        except OSError as error:
            problems.append(f"{label}: {error}")
    if problems:
        return CheckResult("dirs", "FAIL", "; ".join(problems))
    return CheckResult("dirs", "OK", f"{config.paths.music}")


def _check_beets(config: AppConfig) -> CheckResult:
    beet = resolve_beet()
    if beet is None:
        return CheckResult("beets", "WARN", "beet binary not found")
    return CheckResult("beets", "OK", beet)


def _check_musicbrainz(config: AppConfig) -> CheckResult:
    headers = {"User-Agent": f"{APP_NAME}/0.1.0 ({config.musicbrainz_contact or 'unset'})"}
    try:
        response = httpx.get(
            "https://musicbrainz.org/ws/2/release",
            params={"limit": 1},
            headers=headers,
            timeout=8.0,
        )
    except httpx.HTTPError:
        return CheckResult("musicbrainz", "WARN", "unreachable")
    if response.status_code < 500:
        if not config.musicbrainz_contact:
            return CheckResult("musicbrainz", "WARN", "reachable, MUSICBRAINZ_CONTACT unset")
        return CheckResult("musicbrainz", "OK", "reachable")
    return CheckResult("musicbrainz", "WARN", f"HTTP {response.status_code}")


CHECKS = (
    _check_api_key,
    _check_slskd,
    _check_dirs,
    _check_compose,
    _check_beets,
    _check_musicbrainz,
)


def run_all_checks(config: AppConfig) -> list[CheckResult]:
    return [check(config) for check in CHECKS]
