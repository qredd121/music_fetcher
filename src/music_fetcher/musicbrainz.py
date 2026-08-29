from __future__ import annotations

from dataclasses import dataclass

import musicbrainzngs as mbngs

from .config import APP_NAME


@dataclass
class ReleaseSummary:
    mbid: str
    title: str
    artist: str
    date: str
    primary_type: str
    track_count: int | None


@dataclass
class ReleaseTrack:
    position: str
    title: str
    length_ms: int | None


@dataclass
class Release:
    mbid: str
    title: str
    artist: str
    date: str
    tracks: list[ReleaseTrack]

    @property
    def track_count(self) -> int:
        return len(self.tracks)


_client_ready = False


def _configure(contact: str) -> None:
    global _client_ready
    if not _client_ready:
        mbngs.set_useragent(APP_NAME, "0.1.0", contact or "unset@example.com")
        _client_ready = True


def search_releases(
    query: str, *, contact: str, limit: int = 10
) -> list[ReleaseSummary]:
    _configure(contact)
    data = mbngs.search_releases(query=query, limit=limit)
    summaries = []
    for item in data.get("release-list", []):
        medium_list = item.get("medium-list", [])
        track_count = sum(int(m.get("track-count", 0)) for m in medium_list)
        summaries.append(
            ReleaseSummary(
                mbid=item["id"],
                title=item.get("title", ""),
                artist=item.get("artist-credit-phrase", ""),
                date=item.get("date", ""),
                primary_type=item.get("primary-type", ""),
                track_count=track_count or None,
            )
        )
    return summaries


def get_release(mbid: str, *, contact: str) -> Release:
    _configure(contact)
    data = mbngs.get_release_by_id(mbid, includes=["recordings", "media", "artists"])
    release = data["release"]
    artist = (
        release.get("artist-credit-phrase")
        or release.get("artist-credit", [{}])[0].get("artist", {}).get("name", "")
    )
    tracks: list[ReleaseTrack] = []
    for medium in release.get("medium-list", []):
        for track in medium.get("track-list", []):
            recording = track.get("recording", {})
            length = recording.get("length")
            tracks.append(
                ReleaseTrack(
                    position=str(track.get("position", "")),
                    title=track.get("title", ""),
                    length_ms=int(length) if length else None,
                )
            )
    return Release(
        mbid=release["id"],
        title=release.get("title", ""),
        artist=artist,
        date=release.get("date", ""),
        tracks=tracks,
    )
