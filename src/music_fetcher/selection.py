from __future__ import annotations

from dataclasses import dataclass

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


def format_rank(result: SearchResult, chain: list[str]) -> int:
    ranks = [
        _format_rank(f.extension, chain) for f in audio_files(result)
    ]
    return min(ranks) if ranks else len(chain) + 2


def best_format(result: SearchResult, chain: list[str]) -> str:
    files = audio_files(result)
    if not files:
        return "?"
    ranked = sorted(files, key=lambda f: _format_rank(f.extension, chain))
    formats = {f.extension for f in ranked if _format_rank(f.extension, chain) < len(chain)}
    return "/".join(sorted(formats)) if formats else ranked[0].extension


def average_bitrate(result: SearchResult) -> int:
    bitrates = [f.bitrate for f in audio_files(result) if f.bitrate]
    return int(sum(bitrates) / len(bitrates)) if bitrates else 0


@dataclass
class ScoredCandidate:
    result: SearchResult
    format_rank: int
    completeness: float
    bitrate: int

    def score(self, chain: list[str]) -> tuple:
        queue_penalty = 0 if self.result.has_free_slot else 1
        return (
            self.format_rank,
            -self.completeness,
            queue_penalty,
            -self.bitrate,
            -self.result.upload_speed,
        )


def score_candidates(
    results: list[SearchResult],
    chain: list[str],
    expected_tracks: int | None = None,
) -> list[ScoredCandidate]:
    scored = []
    for result in results:
        count = len(audio_files(result))
        if count == 0 or result.total_size == 0:
            continue
        if expected_tracks:
            ratio = count / expected_tracks
            completeness = min(ratio, 1.0) if ratio >= 1 else ratio
        else:
            completeness = min(count / 15.0, 1.0)
        candidate = ScoredCandidate(
            result=result,
            format_rank=format_rank(result, chain),
            completeness=completeness,
            bitrate=average_bitrate(result),
        )
        scored.append(candidate)
    scored.sort(key=lambda c: c.score(chain))
    return scored


def meets_format_preference(candidate: ScoredCandidate, chain: list[str]) -> bool:
    return candidate.format_rank < len(chain)


def best_single_file(result: SearchResult, chain: list[str]) -> SearchFile:
    files = audio_files(result)
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
