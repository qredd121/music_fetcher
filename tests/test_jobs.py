from __future__ import annotations

from music_fetcher.jobs.db import JobStore
from music_fetcher.jobs.runner import Runner, _classify
from music_fetcher.slskd.client import SearchResult


def test_job_lifecycle(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create("track", "q", payload={"username": "u"}, expected_files=1)
    assert job.state == "queued"
    assert store.next_queued().id == job.id

    store.update_state(job.id, "downloading")
    assert store.get(job.id).state == "downloading"
    assert store.next_queued() is None

    store.update_state(
        job.id, "failed", error="boom", set_fields={"folder": "/x", "payload": {"a": 1}}
    )
    reloaded = store.get(job.id)
    assert reloaded.state == "failed"
    assert reloaded.error == "boom"
    assert reloaded.folder == "/x"
    assert reloaded.payload == {"a": 1}

    store.delete(job.id)
    assert store.get(job.id) is None


def test_classify_states():
    assert _classify("Completed, Succeeded") == "succeeded"
    assert _classify("Completed, Errored") == "failed"
    assert _classify("InProgress") == "active"
    assert _classify("Queued, Remotely") == "active"


def test_match_files_exact_names():
    transfers = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "d",
                    "files": [
                        {"filename": "d\\a.flac", "size": 1, "state": "InProgress"},
                        {"filename": "d\\b.flac", "size": 2, "state": "InProgress"},
                    ],
                }
            ],
        },
        {"username": "bob", "directories": []},
    ]
    matched = Runner._match_files(transfers, "alice", "", exact_names={"d\\a.flac"})
    assert len(matched) == 1
    assert matched[0]["filename"] == "d\\a.flac"


def test_search_result_parsing():
    raw = {
        "username": "carol",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 5000,
        "queueLength": 0,
        "files": [
            {"filename": "x\\song.flac", "size": 100, "extension": "flac", "bitRate": 900},
        ],
    }
    result = SearchResult.from_api(raw)
    assert result.username == "carol"
    assert result.files[0].extension == "FLAC"
    assert result.files[0].bitrate == 900
