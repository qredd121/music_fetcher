from __future__ import annotations

import json
import wave
from pathlib import Path

from mediafile import MediaFile

from music_fetcher.tags import (
    PLAN_FILE,
    apply,
    merge_meta,
    parse_dir_name,
    parse_stem,
    resolve,
    write_meta,
)


def make_wav(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\0\0" * 80)
    if tags:
        media = MediaFile(str(path))
        for key, value in tags.items():
            setattr(media, key, value)
        media.save()
    return path


def test_parse_dir_name_variants():
    assert parse_dir_name("(1986) Ночной бит (FLAC)") == {"year": 1986, "album": "Ночной бит"}
    assert parse_dir_name("1991 - Смертное") == {"year": 1991, "album": "Смертное"}
    assert parse_dir_name("CD2 - Внимание") == {"disc": 2, "album": "Внимание"}
    assert parse_dir_name("Artist - 1999 - Album [FLAC]") == {
        "year": 1999, "album": "Album", "albumartist": "Artist",
    }


def test_parse_stem_variants():
    assert parse_stem("01 - Title") == {"track": 1, "title": "Title"}
    assert parse_stem("03. Song Name") == {"track": 3, "title": "Song Name"}
    assert parse_stem("1-03 Song") == {"disc": 1, "track": 3, "title": "Song"}


def test_resolve_complete_tags_is_ok(tmp_path):
    album = tmp_path / "x"
    for n in (1, 2):
        make_wav(album / f"{n}.wav", albumartist="A", artist="A", album="B",
                 year=2000, track=n, title=f"t{n}")
    plan = resolve(album)
    assert plan.ok
    assert all(not e.changes for e in plan.files)


def test_resolve_uses_folder_and_filename_fallbacks(tmp_path):
    album = tmp_path / "(1986) Ночной бит (FLAC)"
    make_wav(album / "01 - Первая.wav", artist="Кино")
    make_wav(album / "02 - Вторая.wav", artist="Кино")
    plan = resolve(album)
    assert plan.ok, plan.to_dict()
    first = plan.files[0].proposed
    assert first["albumartist"] == "Кино"
    assert first["album"] == "Ночной бит"
    assert first["year"] == 1986
    assert (first["track"], first["title"]) == (1, "Первая")


def test_meta_overrides_tags(tmp_path):
    album = tmp_path / "x"
    make_wav(album / "1.wav", albumartist="wrong", album="wrong", year=1990, track=1, title="t")
    meta = {"album": {"albumartist": "Right", "album": "Album", "year": 2001}}
    plan = resolve(album, meta)
    assert plan.ok
    assert plan.files[0].proposed["albumartist"] == "Right"
    assert plan.files[0].changes["year"] == [1990, 2001]


def test_missing_year_is_reported(tmp_path):
    album = tmp_path / "No Year Here"
    make_wav(album / "01 - a.wav", artist="A", album="B")
    plan = resolve(album)
    assert not plan.ok
    assert plan.missing == {"01 - a.wav": ["year"]}


def test_inconsistent_album_is_a_problem(tmp_path):
    album = tmp_path / "x"
    make_wav(album / "1.wav", albumartist="A", album="B", year=2000, track=1, title="a")
    make_wav(album / "2.wav", albumartist="A", album="C", year=2000, track=2, title="b")
    plan = resolve(album)
    assert not plan.ok
    assert any(p.startswith("album differs") for p in plan.problems)


def test_duplicate_track_numbers_is_a_problem(tmp_path):
    album = tmp_path / "x"
    make_wav(album / "a.wav", albumartist="A", album="B", year=2000, track=1, title="a")
    make_wav(album / "b.wav", albumartist="A", album="B", year=2000, track=1, title="b")
    assert any("duplicate" in p for p in resolve(album).problems)


def test_singleton_needs_only_artist_and_title(tmp_path):
    folder = tmp_path / "s"
    make_wav(folder / "song.wav")
    plan = resolve(folder, {"album": {"artist": "A"}}, singletons=True)
    assert plan.ok
    assert plan.files[0].proposed["title"] == "song"


def test_apply_writes_tags(tmp_path):
    album = tmp_path / "x"
    path = make_wav(album / "1.wav", track=1, title="t")
    plan = resolve(album, {"album": {"albumartist": "A", "album": "B", "year": 2002}})
    assert apply(plan) == 1
    media = MediaFile(str(path))
    assert (media.albumartist, media.album, media.year) == ("A", "B", 2002)


def test_meta_file_merges_and_hidden_files_ignored(tmp_path):
    album = tmp_path / "x"
    make_wav(album / "1.wav", artist="A", album="B", track=1, title="t")
    write_meta(album, {"album": {"year": 1999}})
    (album / PLAN_FILE).write_text("{}")
    merged = merge_meta({"album": {"album": "C"}}, json.loads((album / ".mf-meta.json").read_text()))
    plan = resolve(album, merged)
    assert plan.ok
    assert len(plan.files) == 1
    assert plan.files[0].proposed["year"] == 1999
    assert plan.files[0].proposed["album"] == "C"
