from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import beetsbridge
from .config import AppConfig
from .tags import apply, merge_meta, read_meta, resolve, write_plan


@dataclass
class ImportResult:
    source: Path
    state: str
    detail: str
    plan: dict | None = None

    def to_dict(self) -> dict:
        data = {"source": str(self.source), "state": self.state, "detail": self.detail}
        if self.plan is not None:
            data["problems"] = self.plan["problems"]
            data["missing"] = self.plan["missing"]
        return data


def quarantine_root(config: AppConfig) -> Path:
    return config.paths.state / "quarantine"


def in_quarantine(config: AppConfig, path: Path) -> bool:
    return path.resolve().parent == quarantine_root(config).resolve()


def move_to_quarantine(config: AppConfig, source: Path) -> Path:
    if in_quarantine(config, source):
        return source
    root = quarantine_root(config)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / source.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(source), str(dest))
    return dest


def import_source(
    config: AppConfig,
    source: Path,
    meta: dict | None = None,
    *,
    singletons: bool = False,
) -> ImportResult:
    plan = resolve(source, merge_meta(meta, read_meta(source)), singletons=singletons)
    if not plan.ok:
        dest = move_to_quarantine(config, source)
        plan.source = dest
        write_plan(dest, plan)
        reason = "; ".join(plan.problems) or "missing required tags"
        return ImportResult(dest, "quarantined", reason, plan.to_dict())
    apply(plan)
    outcome = beetsbridge.import_path(config, source, singletons=singletons)
    if outcome.state == "imported":
        if in_quarantine(config, source):
            shutil.rmtree(source, ignore_errors=True)
        return ImportResult(source, "imported", outcome.detail)
    if outcome.state == "failed":
        return ImportResult(source, "failed", outcome.detail)
    dest = move_to_quarantine(config, source)
    return ImportResult(dest, "quarantined", outcome.detail)
