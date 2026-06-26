"""SQLite audit log for media-mate.

The audit log is the system of record for "what happened to my media."
Every capability writes here as it runs; queries happen via the `log`
CLI subcommand (see cli.py).

Design choices:
- One connection per LogStore instance; context-manager pattern for transactions
- Schema is created on first connection (CREATE TABLE IF NOT EXISTS)
- Parameterized queries throughout — no string interpolation into SQL
- Pydantic models from models.py are the public API; rows are mapped to models
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from media_mate.models import (
    FileRecord,
    ProbeRecord,
    ProjectRecord,
    ProxyRecord,
    RunRecord,
    RunStatus,
    VerificationRecord,
)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    command TEXT NOT NULL,
    config_hash TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    size INTEGER,
    mtime REAL,
    first_seen_run INTEGER REFERENCES runs(id),
    last_seen_run INTEGER REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES files(id),
    run_id INTEGER REFERENCES runs(id),
    codec TEXT,
    container TEXT,
    width INTEGER,
    height INTEGER,
    frame_rate REAL,
    color_space TEXT,
    bit_depth INTEGER,
    duration REAL,
    audio_channels INTEGER,
    audio_sample_rate INTEGER,
    probed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES files(id),
    proxy_path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    codec TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    resolution TEXT,
    frame_rate TEXT,
    color_space TEXT,
    bin_count INTEGER,
    timeline_count INTEGER,
    resolve_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    files_checked INTEGER,
    files_missing INTEGER,
    files_modified INTEGER,
    files_added INTEGER,
    checksum_algo TEXT,
    verified_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_probes_file_id ON probes(file_id);
CREATE INDEX IF NOT EXISTS idx_probes_run_id ON probes(run_id);
CREATE INDEX IF NOT EXISTS idx_proxies_run_id ON proxies(run_id);
CREATE INDEX IF NOT EXISTS idx_projects_run_id ON projects(run_id);
CREATE INDEX IF NOT EXISTS idx_verifications_run_id ON verifications(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
"""


def _iso(dt: datetime) -> str:
    """Serialize a datetime as an ISO-8601 string in UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string back into a datetime (with UTC tzinfo)."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


class LogStore:
    """SQLite audit log store.

    Usage:
        store = LogStore(Path("~/.media-mate/media-mate.db").expanduser())
        store.initialize()
        run_id = store.start_run("media-mate run ./raw")
        ...
        store.finish_run(run_id, RunStatus.SUCCESS)
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection and ensure it's closed cleanly."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the database file and schema if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def start_run(self, command: str, config_hash: str | None = None) -> int:
        """Insert a new run row in RUNNING status; return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, command, config_hash, status) VALUES (?, ?, ?, ?)",
                (_iso(datetime.now(timezone.utc)), command, config_hash, RunStatus.RUNNING.value),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        error: str | None = None,
    ) -> None:
        """Mark a run as finished with the given status and optional error."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, error = ? WHERE id = ?",
                (_iso(datetime.now(timezone.utc)), status.value, error, run_id),
            )

    def get_run(self, run_id: int) -> RunRecord | None:
        """Fetch a run row by id."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunRecord(
            id=row["id"],
            started_at=_parse_dt(row["started_at"]) or datetime.now(timezone.utc),
            finished_at=_parse_dt(row["finished_at"]),
            command=row["command"],
            config_hash=row["config_hash"],
            status=RunStatus(row["status"]),
            error=row["error"],
        )

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def upsert_file(
        self,
        path: str,
        size: int | None = None,
        mtime: float | None = None,
        run_id: int | None = None,
    ) -> int:
        """Insert a new file row or update last_seen_run on an existing one.

        Returns the file's id (existing or newly created).
        """
        with self._connect() as conn:
            existing = conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
            if existing is not None:
                file_id = int(existing["id"])
                if run_id is not None:
                    conn.execute(
                        "UPDATE files SET last_seen_run = ? WHERE id = ?",
                        (run_id, file_id),
                    )
                return file_id

            cur = conn.execute(
                "INSERT INTO files (path, size, mtime, first_seen_run, last_seen_run) "
                "VALUES (?, ?, ?, ?, ?)",
                (path, size, mtime, run_id, run_id),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def get_file(self, file_id: int) -> FileRecord | None:
        """Fetch a file row by id."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return None
        return FileRecord(
            id=row["id"],
            path=row["path"],
            size=row["size"],
            mtime=row["mtime"],
            first_seen_run=row["first_seen_run"],
            last_seen_run=row["last_seen_run"],
        )

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def insert_probe(self, record: ProbeRecord) -> int:
        """Insert a probe row; return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO probes (file_id, run_id, codec, container, width, height, "
                "frame_rate, color_space, bit_depth, duration, audio_channels, "
                "audio_sample_rate, probed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
                (
                    record.file_id,
                    record.run_id,
                    record.codec,
                    record.container,
                    record.width,
                    record.height,
                    record.frame_rate,
                    record.color_space,
                    record.bit_depth,
                    record.duration,
                    record.audio_channels,
                    record.audio_sample_rate,
                    _iso(record.probed_at),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Proxies
    # ------------------------------------------------------------------

    def insert_proxy(self, record: ProxyRecord) -> int:
        """Insert a proxy row; return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO proxies (source_file_id, proxy_path, run_id, codec, width, "
                "height, file_size, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.source_file_id,
                    record.proxy_path,
                    record.run_id,
                    record.codec,
                    record.width,
                    record.height,
                    record.file_size,
                    _iso(record.generated_at),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def insert_project(self, record: ProjectRecord) -> int:
        """Insert a project row; return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO projects (name, path, run_id, resolution, frame_rate, "
                "color_space, bin_count, timeline_count, resolve_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.name,
                    record.path,
                    record.run_id,
                    record.resolution,
                    record.frame_rate,
                    record.color_space,
                    record.bin_count,
                    record.timeline_count,
                    record.resolve_version,
                    _iso(record.created_at),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Verifications
    # ------------------------------------------------------------------

    def insert_verification(self, record: VerificationRecord) -> int:
        """Insert a verification row; return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO verifications (folder, run_id, files_checked, files_missing, "
                "files_modified, files_added, checksum_algo, verified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.folder,
                    record.run_id,
                    record.files_checked,
                    record.files_missing,
                    record.files_modified,
                    record.files_added,
                    record.checksum_algo,
                    _iso(record.verified_at),
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid


__all__ = ["SCHEMA_VERSION", "LogStore"]
