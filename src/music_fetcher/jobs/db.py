from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    query TEXT NOT NULL,
    mbid TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    username TEXT,
    folder TEXT,
    expected_files INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
"""

ACTIVE_STATES = ("searched", "queued", "downloading")
TERMINAL_STATES = ("downloaded", "imported", "failed", "quarantined", "cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    query: str
    state: str
    mbid: str | None = None
    username: str | None = None
    folder: str | None = None
    expected_files: int | None = None
    error: str | None = None
    payload: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class JobStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def create(
        self,
        kind: str,
        query: str,
        *,
        mbid: str | None = None,
        payload: dict | None = None,
        expected_files: int | None = None,
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            kind=kind,
            query=query,
            state="queued",
            mbid=mbid,
            expected_files=expected_files,
            payload=payload or {},
            created_at=_now(),
            updated_at=_now(),
        )
        self._conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.id,
                job.kind,
                job.query,
                job.mbid,
                json.dumps(job.payload),
                job.state,
                job.username,
                job.folder,
                job.expected_files,
                job.error,
                job.created_at,
                job.updated_at,
            ),
        )
        self._conn.commit()
        return job

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            query=row["query"],
            state=row["state"],
            mbid=row["mbid"],
            username=row["username"],
            folder=row["folder"],
            expected_files=row["expected_files"],
            error=row["error"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self, states: tuple[str, ...] | None = None) -> list[Job]:
        if states:
            placeholders = ",".join("?" * len(states))
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at",
                states,
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def next_queued(self) -> Job | None:
        queued = self.list(states=("queued",))
        return queued[0] if queued else None

    def update_state(
        self,
        job_id: str,
        state: str,
        *,
        error: str | None = None,
        set_fields: dict | None = None,
    ) -> None:
        assignments = ["state=?", "updated_at=?"]
        values: list = [state, _now()]
        if error is not None:
            assignments.append("error=?")
            values.append(error)
        for name, value in (set_fields or {}).items():
            if name not in {"id", "created_at"}:
                assignments.append(f"{name}=?")
                values.append(json.dumps(value) if isinstance(value, dict) else value)
        values.append(job_id)
        self._conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values
        )
        self._conn.commit()

    def delete(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        self._conn.commit()
