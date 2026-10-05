from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from unidecode import unidecode

from .slskd import SearchFile, SearchResult

AUDIO_EXTENSIONS = {
    "FLAC",
    "MP3",
    "OGG",
    "OPUS",
    "M4A",
    "AAC",
    "WAV",
    "AIFF",
    "AIF",
    "WMA",
    "WV",
    "APE",
    "ALAC",
}


def is_audio(file: SearchFile) -> bool:
    return file.extension.upper() in AUDIO_EXTENSIONS


def audio_files(result: SearchResult) -> list[SearchFile]:
    return [f for f in result.files if is_audio(f)]


def _format_rank(extension: str, chain: list[str]) -> int:
    ext = extension.upper()
    for index, entry in enumerate(chain):
        parts = entry.upper().split()
        if ext == parts[0] or ext.startswith(parts[0]):
            return index
    return len(chain) + 1


def _files_format_rank(files: list[SearchFile], chain: list[str]) -> int:
    ranks = [_format_rank(f.extension, chain) for f in files]
    return min(ranks) if ranks else len(chain) + 2


def format_rank(result: SearchResult, chain: list[str]) -> int:
    return _files_format_rank(audio_files(result), chain)


def _files_best_format(files: list[SearchFile], chain: list[str]) -> str:
    if not files:
        return "?"
    ranked = sorted(files, key=lambda f: _format_rank(f.extension, chain))
    formats = {f.extension for f in ranked if _format_rank(f.extension, chain) < len(chain)}
    return "/".join(sorted(formats)) if formats else ranked[0].extension


def best_format(result: SearchResult, chain: list[str]) -> str:
    return _files_best_format(audio_files(result), chain)


def _files_average_bitrate(files: list[SearchFile]) -> int:
    bitrates = [f.bitrate for f in files if f.bitrate]
    return int(sum(bitrates) / len(bitrates)) if bitrates else 0


def average_bitrate(result: SearchResult) -> int:
    return _files_average_bitrate(audio_files(result))


def _dir_of(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    return normalized.rsplit("/", 1)[0] if "/" in normalized else ""


def candidate_id(username: str, folder: str) -> str:
    """Deterministic short id for a (user, folder) source.

    Stable across searches so an agent can pass the id from ``search`` back to
    ``dl`` even though slskd searches are ephemeral. Normalizes the folder path
    (backslashes, trailing slash, case) before hashing.
    """
    normalized = folder.replace("\\", "/").rstrip("/").lower()
    digest = hashlib.sha256(f"{username}\x00{normalized}".encode()).hexdigest()
    return digest[:12]


@dataclass
class ScoredCandidate:
    username: str
    has_free_slot: bool
    upload_speed: int
    folder: str
    files: list[SearchFile]
    format_rank: int
    completeness: float
    bitrate: int
    relevance: float = 0.0

    @property
    def id(self) -> str:
        return candidate_id(self.username, self.folder)

    @property
    def folder_name(self) -> str:
        return self.folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def to_dict(self, chain: list[str]) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "folder": self.folder_name,
            "remote_dir": common_remote_dir(self.files),
            "formats": sorted({f.extension for f in self.files if f.extension}),
            "format_rank": self.format_rank,
            "meets_preference": meets_format_preference(self, chain),
            "file_count": len(self.files),
            "total_size": self.total_size,
            "avg_bitrate": self.bitrate,
            "has_free_slot": self.has_free_slot,
            "upload_speed": self.upload_speed,
            "completeness": round(self.completeness, 4),
            "relevance": round(self.relevance, 4),
            "files": [
                {"filename": f.filename, "size": f.size, "extension": f.extension}
                for f in self.files
            ],
        }

    def score(self, chain: list[str]) -> tuple:
        queue_penalty = 0 if self.has_free_slot else 1
        return (
            -round(self.relevance, 2),
            self.format_rank,
            -self.completeness,
            queue_penalty,
            -self.bitrate,
            -self.upload_speed,
        )


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", unidecode(text).lower()) if len(t) > 1}


def _relevance(
    folder: str,
    files: list[SearchFile],
    expected_terms: set[str],
) -> float:
    if not expected_terms:
        return 0.0
    haystack = _tokenize(folder)
    for file in files[:4]:
        haystack |= _tokenize(file.filename.replace("\\", "/").rsplit("/", 1)[-1])
    if not haystack:
        return 0.0
    return len(expected_terms & haystack) / len(expected_terms)


def _completeness(count: int, expected_tracks: int | None) -> float:
    if expected_tracks:
        if count == expected_tracks:
            return 1.0
        if count < expected_tracks:
            return count / expected_tracks
        return max(0.0, 1.0 - (count - expected_tracks) / expected_tracks)
    return min(count / 15.0, 1.0)


def score_candidates(
    results: list[SearchResult],
    chain: list[str],
    expected_tracks: int | None = None,
    *,
    artist: str = "",
    album: str = "",
) -> list[ScoredCandidate]:
    expected_terms = _tokenize(f"{artist} {album}")
    scored: list[ScoredCandidate] = []
    for result in results:
        groups: dict[str, list[SearchFile]] = {}
        for file in audio_files(result):
            groups.setdefault(_dir_of(file.filename), []).append(file)
        for folder, files in groups.items():
            total_size = sum(f.size for f in files)
            if not files or total_size == 0:
                continue
            scored.append(
                ScoredCandidate(
                    username=result.username,
                    has_free_slot=result.has_free_slot,
                    upload_speed=result.upload_speed,
                    folder=folder,
                    files=files,
                    format_rank=_files_format_rank(files, chain),
                    completeness=_completeness(len(files), expected_tracks),
                    bitrate=_files_average_bitrate(files),
                    relevance=_relevance(folder, files, expected_terms),
                )
            )
    scored.sort(key=lambda c: c.score(chain))
    return scored


def meets_format_preference(candidate: ScoredCandidate, chain: list[str]) -> bool:
    return candidate.format_rank < len(chain)


def best_single_file(files: list[SearchFile], chain: list[str]) -> SearchFile:
    return min(
        files,
        key=lambda f: (
            _format_rank(f.extension, chain),
            -(f.bitrate or 0),
            -f.size,
        ),
    )


def common_remote_dir(files: list[SearchFile]) -> str:
    dirs = set()
    for f in files:
        normalized = f.filename.replace("\\", "/")
        if "/" in normalized:
            dirs.add(normalized.rsplit("/", 1)[0])
    if not dirs:
        return ""
    if len(dirs) == 1:
        return dirs.pop().replace("/", "\\")
    prefix = next(iter(dirs))
    for directory in dirs:
        while prefix and not directory.startswith(prefix):
            prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
    if not prefix:
        prefix = next(iter(dirs))
    return prefix.replace("/", "\\")
