from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mediafile import MediaFile, UnreadableFileError

AUDIO_SUFFIXES = {
    ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".aiff", ".aif",
    ".wma", ".wv", ".ape", ".alac",
}
FIELDS = ("albumartist", "artist", "album", "year", "disc", "track", "title")
ALBUM_FIELDS = ("albumartist", "album", "year")
INT_FIELDS = {"year", "disc", "track"}
REQUIRED_ALBUM = ("albumartist", "album", "year", "track", "title")
REQUIRED_SINGLE = ("artist", "title")
META_FILE = ".mf-meta.json"
PLAN_FILE = ".mf-plan.json"

_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_DISC_DIR = re.compile(r"^(?:cd|disc|disk)\s*[-_.]?\s*(\d{1,2})\b", re.IGNORECASE)
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")
_DISC_TRACK_STEM = re.compile(r"^(\d)-(\d{2})\s*[-._)]?\s*(.+)$")
_TRACK_STEM = re.compile(r"^(\d{1,3})(?:\s*[-._)]\s*|\s+)(.+)$")


def audio_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in AUDIO_SUFFIXES else []
    return sorted(
        p for p in source.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        and not any(part.startswith(".") for part in p.relative_to(source).parts)
    )


def coerce(name: str, value: object) -> object:
    if value is None:
        return None
    if name in INT_FIELDS:
        match = re.match(r"\s*(\d+)", str(value))
        number = int(match.group(1)) if match else 0
        return number or None
    text = str(value).strip()
    return text or None


def _first(*values: object) -> object:
    return next((v for v in values if v is not None), None)


def read_tags(path: Path) -> dict:
    media = MediaFile(str(path))
    return {name: coerce(name, getattr(media, name, None)) for name in FIELDS}


def _strip_dashes(text: str) -> str:
    return text.strip(" -–—_.")


def parse_dir_name(name: str) -> dict:
    guess: dict = {}
    disc = _DISC_DIR.match(name)
    if disc:
        guess["disc"] = int(disc.group(1))
        name = name[disc.end():]
    year = _YEAR.search(name)
    if year:
        guess["year"] = int(year.group(1))
    text = _BRACKETS.sub(" ", name)
    text = _YEAR.sub(" ", text)
    parts = [p for p in (_strip_dashes(s) for s in _DASH_SPLIT.split(text)) if p]
    if not parts:
        parts = [_strip_dashes(text)] if _strip_dashes(text) else []
    if parts:
        guess["album"] = parts[-1]
    if len(parts) >= 2:
        guess["albumartist"] = parts[0]
    return guess


def parse_stem(stem: str) -> dict:
    match = _DISC_TRACK_STEM.match(stem)
    if match:
        disc, track, rest = int(match.group(1)), int(match.group(2)), match.group(3)
        guess: dict = {"disc": disc, "track": track}
    else:
        match = _TRACK_STEM.match(stem)
        if not match:
            return {"title": stem.strip()} if stem.strip() else {}
        guess = {"track": int(match.group(1))}
        rest = match.group(2)
    parts = [p for p in (_strip_dashes(s) for s in _DASH_SPLIT.split(rest)) if p]
    if parts:
        guess["title"] = parts[-1]
    return guess


def _dir_guess(path: Path, source: Path) -> dict:
    guess: dict = {}
    current = path.parent if path.is_file() else path
    chain = [current]
    while current != source and source in current.parents:
        current = current.parent
        chain.append(current)
    for directory in chain:
        for key, value in parse_dir_name(directory.name).items():
            guess.setdefault(key, value)
    return guess


@dataclass
class FileEntry:
    file: str
    path: Path
    current: dict
    proposed: dict
    missing: list[str] = field(default_factory=list)

    @property
    def changes(self) -> dict:
        return {
            name: [self.current.get(name), value]
            for name, value in self.proposed.items()
            if value is not None and value != self.current.get(name)
        }


@dataclass
class Plan:
    source: Path
    singletons: bool
    files: list[FileEntry]
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.problems and not any(e.missing for e in self.files)

    @property
    def missing(self) -> dict:
        return {e.file: e.missing for e in self.files if e.missing}

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "singletons": self.singletons,
            "ok": self.ok,
            "problems": self.problems,
            "missing": self.missing,
            "files": [
                {
                    "index": index,
                    "file": entry.file,
                    "current": entry.current,
                    "proposed": entry.proposed,
                    "changes": entry.changes,
                }
                for index, entry in enumerate(self.files, start=1)
            ],
        }


def _clean_fields(data: dict | None) -> dict:
    return {k: coerce(k, v) for k, v in (data or {}).items() if k in FIELDS}


def merge_meta(*metas: dict | None) -> dict:
    merged: dict = {"album": {}, "files": {}}
    for meta in metas:
        if not meta:
            continue
        merged["album"].update(meta.get("album") or {})
        for name, values in (meta.get("files") or {}).items():
            merged["files"].setdefault(name, {}).update(values or {})
    return merged


def read_meta(source: Path) -> dict:
    path = source / META_FILE
    if not source.is_dir() or not path.exists():
        return {}
    return json.loads(path.read_text())


def write_meta(source: Path, meta: dict) -> None:
    (source / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def write_plan(source: Path, plan: Plan) -> None:
    (source / PLAN_FILE).write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))


def resolve(source: Path, meta: dict | None = None, *, singletons: bool = False) -> Plan:
    meta = meta or {}
    album_meta = _clean_fields(meta.get("album"))
    file_meta = meta.get("files") or {}
    root = source if source.is_dir() else source.parent
    entries: list[FileEntry] = []
    problems: list[str] = []
    guesses: dict[str, dict] = {}

    for path in audio_files(source):
        rel = path.relative_to(root).as_posix()
        try:
            current = read_tags(path)
        except (UnreadableFileError, OSError) as error:
            problems.append(f"{rel}: unreadable ({error})")
            continue
        guess = {**_dir_guess(path, root), **parse_stem(path.stem)}
        guesses[rel] = guess
        override = {**album_meta, **_clean_fields(file_meta.get(rel))}
        proposed = {
            name: _first(override.get(name), current.get(name))
            for name in FIELDS
        }
        for name in ("disc", "track", "title"):
            if proposed[name] is None:
                proposed[name] = guess.get(name)
        entries.append(FileEntry(rel, path, current, proposed))

    if not singletons and entries:
        artists = {e.proposed["artist"] for e in entries}
        common_artist = artists.pop() if len(artists) == 1 else None
        for entry in entries:
            guess = guesses[entry.file]
            if entry.proposed["albumartist"] is None:
                entry.proposed["albumartist"] = _first(common_artist, guess.get("albumartist"))
            for name in ("album", "year"):
                if entry.proposed[name] is None:
                    entry.proposed[name] = guess.get(name)
        for name in ALBUM_FIELDS:
            values = {e.proposed[name] for e in entries if e.proposed[name] is not None}
            if len(values) > 1:
                shown = ", ".join(str(v) for v in sorted(values, key=str))
                problems.append(f"{name} differs across files: {shown}")
        slots = Counter((e.proposed["disc"] or 1, e.proposed["track"]) for e in entries)
        clashes = sorted(
            f"{disc}-{track}" for (disc, track), n in slots.items()
            if n > 1 and track is not None
        )
        if clashes:
            problems.append("duplicate disc-track numbers: " + ", ".join(clashes))

    for entry in entries:
        if entry.proposed["artist"] is None:
            entry.proposed["artist"] = entry.proposed["albumartist"]
        required = REQUIRED_SINGLE if singletons else REQUIRED_ALBUM
        entry.missing = [name for name in required if entry.proposed[name] is None]

    if not entries and not problems:
        problems.append("no audio files")
    return Plan(source=source, singletons=singletons, files=entries, problems=problems)


def apply(plan: Plan) -> int:
    written = 0
    for entry in plan.files:
        changes = entry.changes
        if not changes:
            continue
        media = MediaFile(str(entry.path))
        for name, (_, value) in changes.items():
            setattr(media, name, value)
        media.save()
        entry.current = {**entry.current, **{k: v for k, (_, v) in changes.items()}}
        written += 1
    return written
