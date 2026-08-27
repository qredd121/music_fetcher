from __future__ import annotations

import json
import subprocess
from pathlib import Path


class ComposeError(RuntimeError):
    pass


def find_compose_file(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
            path = candidate / name
            if path.exists():
                return path
    raise ComposeError("docker-compose.yml not found; run from the project directory")


def _run_compose(compose_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ComposeError(result.stderr.strip() or f"docker compose {args[0]} failed")
    return result


def up() -> None:
    _run_compose(find_compose_file(), "up", "-d")


def down() -> None:
    _run_compose(find_compose_file(), "down")


def ps() -> list[dict]:
    result = _run_compose(find_compose_file(), "ps", "--format", "json")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    services = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, list):
            services.extend(item)
        else:
            services.append(item)
    return services


def logs(follow: bool = False, tail: int | None = None) -> None:
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    compose_file = find_compose_file()
    command = ["docker", "compose", "-f", str(compose_file), *args]
    raise SystemExit(subprocess.run(command, check=False).returncode)
