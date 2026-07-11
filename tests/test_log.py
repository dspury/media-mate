"""Tests for the SQLite audit log in log.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from media_mate.log import SCHEMA_VERSION, LogStore
from media_mate.models import (
    OrganizeOpRecord,
    ProbeRecord,
    ProjectRecord,
    ProxyRecord,
    RunStatus,
    VerificationRecord,
)


@pytest.fixture
def store(tmp_path) -> LogStore:
    s = LogStore(tmp_path / "media-mate.db")
    s.initialize()
    return s


class TestSchema:
    def test_initialize_creates_db(self, store: LogStore, tmp_path) -> None:
        assert (tmp_path / "media-mate.db").exists()

    def test_schema_version_recorded(self, store: LogStore) -> None:
        run = store.start_run("media-mate test")
        assert run > 0  # just exercising the connection works
        # verify schema_meta row
        import sqlite3

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            assert row is not None
            assert row[0] == str(SCHEMA_VERSION)

    def test_initialize_is_idempotent(self, tmp_path) -> None:
        s = LogStore(tmp_path / "media-mate.db")
        s.initialize()
        s.initialize()  # second call must not raise
        assert (tmp_path / "media-mate.db").exists()

    def test_initialize_migrates_legacy_organize_operations(self, tmp_path) -> None:
        """The v0.2.2 audit column is added to pre-existing databases."""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE organize_ops ("
                "id INTEGER PRIMARY KEY, run_id INTEGER, source_path TEXT NOT NULL, "
                "destination_path TEXT NOT NULL, codec_family TEXT, "
                "resolution_bucket TEXT, file_size INTEGER, moved_at TEXT NOT NULL)"
            )

        legacy = LogStore(db_path)
        legacy.initialize()
        run_id = legacy.start_run("media-mate organize ./raw")
        legacy.insert_organize_op(
            OrganizeOpRecord(
                run_id=run_id,
                source_path="/in/clip.mov",
                destination_path="/out/clip.mov",
                codec_family="prores",
                resolution_bucket="1080p",
                file_size=1024,
                moved_at=datetime.now(UTC),
            )
        )

        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(organize_ops)")}
            operation = conn.execute("SELECT operation FROM organize_ops").fetchone()[0]
        assert "operation" in columns
        assert operation == "copy"


class TestRuns:
    def test_start_run_returns_id(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate probe ./raw")
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_finish_run_marks_status(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate probe ./raw")
        store.finish_run(run_id, RunStatus.SUCCESS)
        record = store.get_run(run_id)
        assert record is not None
        assert record.status == RunStatus.SUCCESS
        assert record.finished_at is not None

    def test_finish_run_with_error(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate probe ./raw")
        store.finish_run(run_id, RunStatus.FAILED, error="boom")
        record = store.get_run(run_id)
        assert record is not None
        assert record.status == RunStatus.FAILED
        assert record.error == "boom"

    def test_get_run_missing_returns_none(self, store: LogStore) -> None:
        assert store.get_run(99999) is None

    def test_start_run_persists_config_hash(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate proxy ./raw", config_hash="abc123def456")
        record = store.get_run(run_id)
        assert record is not None
        assert record.config_hash == "abc123def456"

    def test_start_run_config_hash_optional(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate probe ./raw")
        record = store.get_run(run_id)
        assert record is not None
        assert record.config_hash is None


class TestFiles:
    def test_upsert_file_creates_new(self, store: LogStore) -> None:
        fid = store.upsert_file("/tmp/clip.mov", size=1024, mtime=12345.0)
        assert fid > 0
        record = store.get_file(fid)
        assert record is not None
        assert record.path == "/tmp/clip.mov"
        assert record.size == 1024

    def test_upsert_file_returns_existing_id(self, store: LogStore) -> None:
        fid1 = store.upsert_file("/tmp/clip.mov")
        fid2 = store.upsert_file("/tmp/clip.mov")
        assert fid1 == fid2

    def test_upsert_updates_last_seen_run(self, store: LogStore) -> None:
        run1 = store.start_run("media-mate probe ./raw")
        fid = store.upsert_file("/tmp/clip.mov", run_id=run1)
        record = store.get_file(fid)
        assert record is not None
        assert record.first_seen_run == run1
        assert record.last_seen_run == run1

        run2 = store.start_run("media-mate probe ./raw")
        store.upsert_file("/tmp/clip.mov", run_id=run2)
        record2 = store.get_file(fid)
        assert record2 is not None
        assert record2.first_seen_run == run1  # unchanged
        assert record2.last_seen_run == run2  # updated


class TestProbes:
    def test_insert_probe(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate probe ./raw")
        file_id = store.upsert_file("/tmp/clip.mov", run_id=run_id)
        now = datetime.now(UTC)
        pid = store.insert_probe(
            ProbeRecord(
                file_id=file_id,
                run_id=run_id,
                codec="h264",
                container="mov",
                width=1920,
                height=1080,
                frame_rate=23.976,
                color_space="bt709",
                bit_depth=8,
                duration=120.5,
                audio_channels=2,
                audio_sample_rate=48000,
                probed_at=now,
            )
        )
        assert pid > 0


class TestProxies:
    def test_insert_proxy(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate proxy ./raw")
        file_id = store.upsert_file("/tmp/clip.mov", run_id=run_id)
        pid = store.insert_proxy(
            ProxyRecord(
                source_file_id=file_id,
                proxy_path="/tmp/proxy.mov",
                run_id=run_id,
                codec="ProRes422Proxy",
                width=1920,
                height=1080,
                file_size=1048576,
                generated_at=datetime.now(UTC),
            )
        )
        assert pid > 0


class TestProjects:
    def test_insert_project(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate resolve create ./raw")
        pid = store.insert_project(
            ProjectRecord(
                name="Episode-12",
                path="/tmp/Episode-12.drp",
                run_id=run_id,
                resolution="1080",
                frame_rate="24",
                color_space="Rec.709",
                bin_count=5,
                timeline_count=1,
                resolve_version="20.0",
                created_at=datetime.now(UTC),
            )
        )
        assert pid > 0


class TestVerifications:
    def test_insert_verification(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate verify ./raw")
        vid = store.insert_verification(
            VerificationRecord(
                folder="/tmp/raw",
                run_id=run_id,
                files_checked=10,
                files_missing=0,
                files_modified=0,
                files_added=0,
                checksum_algo="xxhash",
                verified_at=datetime.now(UTC),
            )
        )
        assert vid > 0


class TestOrganizeOps:
    def test_insert_organize_op(self, store: LogStore) -> None:
        run_id = store.start_run("media-mate organize ./raw")
        oid = store.insert_organize_op(
            OrganizeOpRecord(
                run_id=run_id,
                source_path="/in/clip.mov",
                destination_path="/out/prores/1080p/clip.mov",
                codec_family="prores",
                resolution_bucket="1080p",
                file_size=1024,
                moved_at=datetime.now(UTC),
            )
        )
        assert oid > 0


class TestQueries:
    def test_get_latest_probes_by_paths_empty(self, store: LogStore) -> None:
        assert store.get_latest_probes_by_paths([]) == {}

    def test_get_latest_probes_returns_latest(self, store: LogStore) -> None:
        run1 = store.start_run("probe 1")
        fid = store.upsert_file("/tmp/clip.mov", run_id=run1)
        store.insert_probe(
            ProbeRecord(
                file_id=fid,
                run_id=run1,
                codec="h264",
                container="mov",
                width=1920,
                height=1080,
                frame_rate=24.0,
                color_space="bt709",
                bit_depth=8,
                duration=60.0,
                audio_channels=2,
                audio_sample_rate=48000,
                probed_at=datetime.now(UTC),
            )
        )
        # A later, different probe for the same file
        run2 = store.start_run("probe 2")
        store.insert_probe(
            ProbeRecord(
                file_id=fid,
                run_id=run2,
                codec="h264",
                container="mov",
                width=3840,
                height=2160,
                frame_rate=30.0,
                color_space="bt2020",
                bit_depth=10,
                duration=60.0,
                audio_channels=2,
                audio_sample_rate=48000,
                probed_at=datetime.now(UTC),
            )
        )

        results = store.get_latest_probes_by_paths(["/tmp/clip.mov"])
        assert "/tmp/clip.mov" in results
        assert results["/tmp/clip.mov"].height == 2160  # latest wins
        assert results["/tmp/clip.mov"].frame_rate == 30.0

    def test_get_latest_probes_omits_missing(self, store: LogStore) -> None:
        results = store.get_latest_probes_by_paths(["/nonexistent/clip.mov"])
        assert results == {}

    def test_get_latest_probes_multiple_files(self, store: LogStore) -> None:
        run_id = store.start_run("probe batch")
        fid_a = store.upsert_file("/a.mov", run_id=run_id)
        fid_b = store.upsert_file("/b.mov", run_id=run_id)
        now = datetime.now(UTC)
        for fid in (fid_a, fid_b):
            store.insert_probe(
                ProbeRecord(
                    file_id=fid,
                    run_id=run_id,
                    codec="h264",
                    container="mov",
                    width=1920,
                    height=1080,
                    frame_rate=24.0,
                    color_space="bt709",
                    bit_depth=8,
                    duration=60.0,
                    audio_channels=2,
                    audio_sample_rate=48000,
                    probed_at=now,
                )
            )

        results = store.get_latest_probes_by_paths(["/a.mov", "/b.mov", "/c.mov"])
        assert set(results.keys()) == {"/a.mov", "/b.mov"}


class TestContextManager:
    def test_connection_closed_on_success(self, store: LogStore) -> None:
        store.start_run("test")
        # If context manager didn't close properly, the next call would block on lock.

    def test_rollback_on_error(self, store: LogStore, tmp_path) -> None:
        """A failing transaction must not leave partial state."""
        import sqlite3

        s = LogStore(tmp_path / "rollback.db")
        s.initialize()

        with pytest.raises(RuntimeError, match="simulated failure"), s._connect() as conn:
            conn.execute(
                "INSERT INTO runs (started_at, command, status) VALUES (?, ?, ?)",
                ("2026-01-01T00:00:00", "x", "running"),
            )
            # Force an error mid-transaction
            raise RuntimeError("simulated failure")

        # After rollback, no runs should exist
        with sqlite3.connect(s.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            assert count == 0
