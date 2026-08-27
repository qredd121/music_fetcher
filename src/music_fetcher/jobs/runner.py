from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from ..config import AppConfig
from ..slskd import SearchFile, SlskdClient, SlskdError
from .db import Job, JobStore

ProgressCallback = Callable[[int, int], None]

SUCCESS_MARKERS = ("Succeeded",)
FAILURE_MARKERS = ("Errored", "Cancelled", "Rejected", "TimedOut", "Aborted")


def _classify(state: str) -> str:
    lowered = state.lower()
    if any(marker.lower() in lowered for marker in SUCCESS_MARKERS):
        return "succeeded"
    if any(marker.lower() in lowered for marker in FAILURE_MARKERS):
        return "failed"
    return "active"


class Runner:
    def __init__(
        self,
        config: AppConfig,
        store: JobStore,
        client: SlskdClient | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client or SlskdClient(config.slskd)

    @staticmethod
    def _match_files(
        transfers: list[dict],
        username: str,
        remote_dir: str,
        exact_names: set[str] | None = None,
    ) -> list[dict]:
        matched = []
        normalized_dir = remote_dir.replace("\\", "/")
        for user_entry in transfers:
            if user_entry.get("username") != username:
                continue
            for directory in user_entry.get("directories", []):
                for file in directory.get("files", []):
                    filename = file.get("filename", "")
                    normalized = filename.replace("\\", "/")
                    if exact_names:
                        if filename in exact_names or normalized in {
                            name.replace("\\", "/") for name in exact_names
                        }:
                            matched.append(file)
                        continue
                    if filename.startswith(remote_dir) or normalized.startswith(
                        normalized_dir
                    ):
                        matched.append(file)
        return matched

    def _snapshot(self, files: list[dict]) -> tuple[int, int, Counter]:
        done = sum(int(f.get("bytesTransferred") or 0) for f in files)
        total = sum(int(f.get("size") or 0) for f in files)
        states: Counter = Counter(_classify(str(f.get("state", ""))) for f in files)
        return done, total, states

    def _cancel_files(self, username: str, files: list[dict]) -> None:
        for file in files:
            try:
                self._client.cancel_transfer(username, file.get("filename", ""))
            except SlskdError:
                continue

    def run_job(
        self,
        job: Job,
        *,
        progress: ProgressCallback | None = None,
    ) -> Job:
        try:
            return self._run_job_inner(job, progress=progress)
        except SlskdError as error:
            self._store.update_state(job.id, "failed", error=str(error))
            return self._require(job.id)

    def _run_job_inner(
        self,
        job: Job,
        *,
        progress: ProgressCallback | None = None,
    ) -> Job:
        username = job.payload.get("username", "")
        remote_dir = job.payload.get("remote_dir", "")
        files_payload = job.payload.get("files", [])
        exact_names = (
            set(job.payload.get("filenames", []))
            if job.payload.get("filenames")
            else None
        )
        if not username or not files_payload:
            self._store.update_state(
                job.id, "failed", error="job payload missing username/files"
            )
            return self._require(job.id)

        self._store.update_state(job.id, "downloading")
        attempts = int(job.payload.get("attempts", 0))

        presence = self._client.user_presence(username)
        if presence == "Offline":
            self._store.update_state(
                job.id, "failed", error=f"user {username} is offline"
            )
            return self._require(job.id)

        existing = self._match_files(
            self._client.transfers(), username, remote_dir, exact_names=exact_names
        )
        present = {(f.get("filename"), f.get("size")) for f in existing}
        missing = [
            f for f in files_payload if (f["filename"], f["size"]) not in present
        ]
        if missing:
            self._client.enqueue(
                username,
                [SearchFile(filename=f["filename"], size=f["size"]) for f in missing],
            )

        deadline = time.monotonic() + self._config.download.timeout
        queue_deadline = time.monotonic() + self._config.download.queue_wait_seconds
        last_bytes = -1
        last_change = time.monotonic()
        polls = 0

        while True:
            time.sleep(1.0)
            polls += 1
            files = self._match_files(
                self._client.transfers(), username, remote_dir, exact_names=exact_names
            )
            done, total, states = self._snapshot(files)

            if progress and total:
                progress(done, total)

            if done > last_bytes:
                last_bytes = done
                last_change = time.monotonic()

            if not files and polls > 30:
                self._store.update_state(
                    job.id, "failed", error="transfer never registered with slskd"
                )
                return self._require(job.id)

            stalled = (
                done > 0
                and time.monotonic() - last_change > self._config.download.stall_seconds
            )
            never_started = done == 0 and time.monotonic() > queue_deadline
            timed_out = time.monotonic() > deadline

            if states.get("active", 0) == 0 or stalled or never_started or timed_out:
                if (
                    states.get("failed", 0) == 0
                    and not stalled
                    and not never_started
                    and not timed_out
                ):
                    self._finish_success(job, username)
                    return self._require(job.id)
                if stalled:
                    self._cancel_files(username, files)
                attempts += 1
                if attempts > self._config.download.retries:
                    reason = (
                        "timed out"
                        if timed_out
                        else "stalled"
                        if stalled
                        else "transfer never started"
                    )
                    self._store.update_state(job.id, "failed", error=reason)
                    return self._require(job.id)
                payload = {**job.payload, "attempts": attempts}
                self._store.update_state(
                    job.id, "queued", set_fields={"payload": payload}
                )
                time.sleep(3.0)
                return self.run_job(
                    self._require(job.id), progress=progress
                )

    def _locate_staged(self, files_payload: list[dict]) -> Path | None:
        wanted = {
            Path(f["filename"].replace("\\", "/")).name: int(f.get("size") or 0)
            for f in files_payload
        }
        root = Path(self._config.paths.downloads)
        if not root.exists():
            return None
        found_dirs = []
        for path in root.rglob("*"):
            if path.is_file() and path.name in wanted and path.stat().st_size == wanted[path.name]:
                found_dirs.append(path.parent)
        if not found_dirs:
            return None
        common = Path(os.path.commonpath([str(d) for d in found_dirs]))
        return common

    def _finish_success(self, job: Job, username: str) -> None:
        staged = self._locate_staged(job.payload.get("files", []))
        if staged is None:
            remote_dir = job.payload.get("remote_dir", "")
            leaf = remote_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            staged = Path(self._config.paths.downloads) / username / leaf
        self._store.update_state(
            job.id,
            "downloaded",
            set_fields={"username": username, "folder": str(staged)},
        )

    def _require(self, job_id: str) -> Job:
        job = self._store.get(job_id)
        assert job is not None
        return job

    def run_all(self, *, progress: ProgressCallback | None = None) -> list[Job]:
        processed = []
        while True:
            pending = self._store.list(states=("downloading", "queued"))
            if not pending:
                break
            job = next(
                (j for j in pending if j.state == "downloading"),
                pending[0],
            )
            final = self.run_job(job, progress=progress)
            processed.append(final)
        return processed
