from __future__ import annotations

from music_fetcher.selection import (
    audio_files,
    best_single_file,
    common_remote_dir,
    format_rank,
    meets_format_preference,
    score_candidates,
)
from music_fetcher.slskd.client import SearchFile, SearchResult

CHAIN = ["FLAC", "MP3 320", "MP3 V0"]


def _result(username, files, slot=True, speed=1000):
    return SearchResult(
        username=username,
        has_free_slot=slot,
        upload_speed=speed,
        queue_length=0,
        files=[SearchFile(filename=f, size=s, extension=e) for f, s, e in files],
    )


def test_audio_files_filters_non_audio():
    result = _result(
        "u",
        [
            ("a\\track.flac", 100, "FLAC"),
            ("a\\cover.jpg", 10, "JPG"),
            ("a\\notes.txt", 5, "TXT"),
        ],
    )
    assert len(audio_files(result)) == 1


def test_format_rank_prefers_flac():
    flac = _result("u", [("a\\t.flac", 100, "FLAC")])
    mp3 = _result("u", [("a\\t.mp3", 100, "MP3")])
    assert format_rank(flac, CHAIN) < format_rank(mp3, CHAIN)


def test_meets_format_preference():
    flac = score_candidates([_result("u", [("a\\t.flac", 100, "FLAC")])], CHAIN)[0]
    wav = score_candidates([_result("u", [("a\\t.wav", 100, "WAV")])], CHAIN)[0]
    assert meets_format_preference(flac, CHAIN)
    assert not meets_format_preference(wav, CHAIN)


def test_score_ranks_complete_flac_first():
    good = _result("good", [(f"a\\{i}.flac", 100, "FLAC") for i in range(10)])
    partial = _result("partial", [("a\\1.mp3", 100, "MP3")])
    ranked = score_candidates([partial, good], CHAIN, expected_tracks=10)
    assert ranked[0].username == "good"


def test_best_single_file_picks_flac():
    result = _result(
        "u", [("a\\t.mp3", 100, "MP3"), ("a\\t.flac", 200, "FLAC")]
    )
    assert best_single_file(result.files, CHAIN).extension == "FLAC"


def test_common_remote_dir_single():
    files = [SearchFile("root\\album\\1.flac", 1, "FLAC"),
             SearchFile("root\\album\\2.flac", 1, "FLAC")]
    assert common_remote_dir(files) == "root\\album"


def test_common_remote_dir_disjoint_falls_back():
    files = [SearchFile("a\\x\\1.flac", 1, "FLAC"),
             SearchFile("b\\y\\2.flac", 1, "FLAC")]
    assert common_remote_dir(files) in ("a\\x", "b\\y")


def test_score_splits_response_by_folder():
    peer = _result(
        "u",
        [
            ("album1\\01.flac", 100, "FLAC"),
            ("album1\\02.flac", 100, "FLAC"),
            ("album2\\10.flac", 100, "FLAC"),
        ],
    )
    cands = score_candidates([peer], CHAIN, expected_tracks=2)
    folders = {c.folder for c in cands}
    assert folders == {"album1", "album2"}
    assert all(len(c.files) <= 2 for c in cands)


def test_relevance_prefers_matching_artist_album():
    right = _result(
        "good",
        [(f"I Hate Myself - 10 Songs\\{i:02d} track.flac", 100, "FLAC") for i in range(10)],
    )
    wrong = _result(
        "bad",
        [(f"VA - Summertime Blues - 10 Songs\\{i:02d} someone.flac", 100, "FLAC") for i in range(10)],
    )
    ranked = score_candidates(
        [wrong, right], CHAIN, expected_tracks=10, artist="I Hate Myself", album="10 Songs"
    )
    assert ranked[0].username == "good"
