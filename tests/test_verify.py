"""Tests for the verify capability in verify.py."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC
from pathlib import Path

import pytest
import xxhash

from media_mate.log import LogStore
from media_mate.models import (
    ChecksumAlgo,
    MediaMateConfig,
    VerificationSnapshotRecord,
)
from media_mate.verify import (
    VerifyError,
    _exit_code,
    _iter_files,
    compute_checksum,
    verify_folder,
)

# ---------------------------------------------------------------------------
# compute_checksum tests
# ---------------------------------------------------------------------------


class TestComputeChecksum:
    def test_xxhash_matches_independent_calculation(self, tmp_path: Path) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(b"hello world")
        expected = xxhash.xxh64(b"hello world").hexdigest()
        assert compute_checksum(p, ChecksumAlgo.XXHASH) == expected

    def test_sha256_matches_hashlib(self, tmp_path: Path) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert compute_checksum(p, ChecksumAlgo.SHA256) == expected

    def test_streams_large_file(self, tmp_path: Path) -> None:
        """A file larger than the chunk size should produce the same checksum as one shot."""
        p = tmp_path / "big.bin"
        data = b"x" * (256 * 1024)  # 256 KB > 64 KB chunk
        p.write_bytes(data)
        expected = xxhash.xxh64(data).hexdigest()
        assert compute_checksum(p) == expected

    def test_different_content_different_checksum(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"foo")
        b.write_bytes(b"bar")
        assert compute_checksum(a) != compute_checksum(b)

    def test_path_does_not_exist(self, tmp_path: Path) -> None:
        with pytest.raises(VerifyError):
            compute_checksum(tmp_path / "missing")

    def test_path_is_directory(self, tmp_path: Path) -> None:
        with pytest.raises(VerifyError):
            compute_checksum(tmp_path)

    def test_default_algo_is_xxhash(self, tmp_path: Path) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(b"x")
        assert compute_checksum(p) == compute_checksum(p, ChecksumAlgo.XXHASH)


# ---------------------------------------------------------------------------
# _exit_code tests
# ---------------------------------------------------------------------------


class TestExitCode:
    def test_clean(self) -> None:
        assert _exit_code(missing=False, modified=False, added=False) == 0

    def test_missing(self) -> None:
        assert _exit_code(missing=True, modified=False, added=False) == 1

    def test_modified(self) -> None:
        assert _exit_code(missing=False, modified=True, added=False) == 2

    def test_added(self) -> None:
        assert _exit_code(missing=False, modified=False, added=True) == 3

    def test_missing_takes_priority_over_modified(self) -> None:
        assert _exit_code(missing=True, modified=True, added=False) == 1

    def test_missing_takes_priority_over_added(self) -> None:
        assert _exit_code(missing=True, modified=False, added=True) == 1

    def test_modified_takes_priority_over_added(self) -> None:
        assert _exit_code(missing=False, modified=True, added=True) == 2

    def test_all_three_reports_as_missing(self) -> None:
        assert _exit_code(missing=True, modified=True, added=True) == 1


# ---------------------------------------------------------------------------
# _iter_files tests
# ---------------------------------------------------------------------------


class TestIterFiles:
    def test_empty_dir(self, tmp_path: Path) -> None:
        assert list(_iter_files(tmp_path)) == []

    def test_top_level_files(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_bytes(b"a")
        (tmp_path / "b").write_bytes(b"b")
        files = [str(p.relative_to(tmp_path)) for p in _iter_files(tmp_path)]
        assert files == ["a", "b"]

    def test_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_bytes(b"a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b").write_bytes(b"b")
        files = [str(p.relative_to(tmp_path)) for p in _iter_files(tmp_path)]
        assert files == ["a", "sub/b"]

    def test_skips_subdirectories(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a").write_bytes(b"a")
        files = list(_iter_files(tmp_path))
        assert len(files) == 1
        assert files[0].name == "a"

    def test_sorted(self, tmp_path: Path) -> None:
        for name in ["z", "a", "m"]:
            (tmp_path / name).write_bytes(b"x")
        names = [p.name for p in _iter_files(tmp_path)]
        assert names == ["a", "m", "z"]


# ---------------------------------------------------------------------------
# verify_folder tests
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media_mate_store")


def _make_store(store_dir: Path) -> LogStore:
    store_dir.mkdir(parents=True, exist_ok=True)
    s = LogStore(store_dir / "log.db")
    s.initialize()
    return s


def _count_rows(store: LogStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class TestVerifyFolder:
    def test_source_not_directory(self, tmp_path: Path, store_dir: Path) -> None:
        f = tmp_path / "clip.mov"
        f.write_bytes(b"x")
        store = _make_store(store_dir)
        with pytest.raises(VerifyError):
            verify_folder(f, store)

    def test_source_does_not_exist(self, tmp_path: Path, store_dir: Path) -> None:
        store = _make_store(store_dir)
        with pytest.raises(VerifyError):
            verify_folder(tmp_path / "nope", store)

    def test_first_run_creates_snapshot_clean(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        (folder / "b").write_bytes(b"world")
        store = _make_store(store_dir)

        report = verify_folder(folder, store)

        assert report.files_checked == 2
        assert report.files_missing == 0
        assert report.files_modified == 0
        assert report.files_added == 0
        assert report.exit_code == 0
        assert report.is_clean is True

        # Snapshot rows were written
        assert _count_rows(store, "verification_snapshots") == 2

        # Verification log row was written
        assert _count_rows(store, "verifications") == 1

    def test_second_run_no_changes_clean(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        verify_folder(folder, store)  # first run
        report = verify_folder(folder, store)  # second run, no changes

        assert report.exit_code == 0
        assert report.is_clean is True
        assert report.files_checked == 1
        assert report.files_added == 0
        assert report.files_modified == 0
        assert report.files_missing == 0

    def test_detects_added_files(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        # Add a new file
        (folder / "b").write_bytes(b"new")

        report = verify_folder(folder, store)

        assert report.files_added == 1
        assert report.exit_code == 3
        assert report.is_clean is False

    def test_detects_modified_files(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        # Modify the file
        (folder / "a").write_bytes(b"changed!")

        report = verify_folder(folder, store)

        assert report.files_modified == 1
        assert report.exit_code == 2
        assert report.is_clean is False

    def test_detects_missing_files(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        (folder / "b").write_bytes(b"world")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        # Delete a file
        (folder / "b").unlink()

        report = verify_folder(folder, store)

        assert report.files_missing == 1
        assert report.exit_code == 1
        assert report.is_clean is False

    def test_detects_combined_diffs_with_priority(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        (folder / "b").write_bytes(b"world")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        # Delete b, modify a, add c
        (folder / "b").unlink()
        (folder / "a").write_bytes(b"changed!")
        (folder / "c").write_bytes(b"new")

        report = verify_folder(folder, store)

        # All three categories present, but missing wins
        assert report.files_missing == 1
        assert report.files_modified == 1
        assert report.files_added == 1
        assert report.exit_code == 1  # missing takes priority

    def test_recursive_scan(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "top").write_bytes(b"x")
        sub = folder / "sub"
        sub.mkdir()
        (sub / "deep").write_bytes(b"y")
        store = _make_store(store_dir)

        report = verify_folder(folder, store)

        assert report.files_checked == 2

    def test_uses_sha256_from_config(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        cfg = MediaMateConfig(checksum_algo=ChecksumAlgo.SHA256)
        report = verify_folder(folder, store, config=cfg)

        # Snapshot algo should match config
        snapshot = store.get_verification_snapshot(str(folder.resolve()))
        assert snapshot[0].algo == "sha256"
        assert report.checksum_algo == ChecksumAlgo.SHA256

    def test_algo_mismatch_raises(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        # First run with xxhash
        verify_folder(folder, store)

        # Second run with sha256 — mismatch
        cfg = MediaMateConfig(checksum_algo=ChecksumAlgo.SHA256)
        with pytest.raises(VerifyError) as exc_info:
            verify_folder(folder, store, config=cfg)
        assert "existing baseline" in exc_info.value.args[0]

    def test_snapshot_replaces_old_state(self, tmp_path: Path, store_dir: Path) -> None:
        """A mismatch keeps the known-good snapshot until explicitly accepted."""
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"v1")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        # Modify a
        (folder / "a").write_bytes(b"v2")
        changed = verify_folder(folder, store)
        assert changed.exit_code == 2

        # Third run reports the same mismatch because the baseline is immutable.
        report = verify_folder(folder, store)

        assert report.exit_code == 2
        assert report.files_modified == 1

    def test_empty_folder(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "empty"
        folder.mkdir()
        store = _make_store(store_dir)

        report = verify_folder(folder, store)

        assert report.files_checked == 0
        assert report.exit_code == 0
        assert _count_rows(store, "verification_snapshots") == 0

    def test_writes_verification_log_row(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT folder, files_checked, checksum_algo FROM verifications LIMIT 1"
            ).fetchone()
            assert row[0] == str(folder.resolve())
            assert row[1] == 1
            assert row[2] == "xxhash"

    def test_path_resolved(self, tmp_path: Path, store_dir: Path) -> None:
        """The folder path should be stored as resolved (absolute, no symlinks)."""
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"x")
        store = _make_store(store_dir)

        verify_folder(folder, store)

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute("SELECT DISTINCT folder FROM verification_snapshots").fetchone()
            assert row[0] == str(folder.resolve())


class TestSnapshotRecordModel:
    def test_construction(self) -> None:
        from datetime import datetime

        rec = VerificationSnapshotRecord(
            folder="/data",
            path="/data/clip.mov",
            checksum="abc123",
            size=1024,
            mtime=12345.0,
            algo="xxhash",
            recorded_at=datetime.now(UTC),
        )
        assert rec.folder == "/data"
        assert rec.path == "/data/clip.mov"
        assert rec.checksum == "abc123"

    def test_log_roundtrip(self, store_dir: Path) -> None:
        from datetime import datetime

        store = _make_store(store_dir)
        rec = VerificationSnapshotRecord(
            folder="/data",
            path="/data/clip.mov",
            checksum="abc123",
            size=1024,
            mtime=12345.0,
            algo="xxhash",
            recorded_at=datetime.now(UTC),
        )
        store.replace_verification_snapshot("/data", [rec])
        loaded = store.get_verification_snapshot("/data")
        assert len(loaded) == 1
        assert loaded[0].path == "/data/clip.mov"
        assert loaded[0].checksum == "abc123"
        assert loaded[0].algo == "xxhash"

    def test_replace_deletes_old(self, store_dir: Path) -> None:
        from datetime import datetime

        store = _make_store(store_dir)
        old = VerificationSnapshotRecord(
            folder="/data",
            path="/data/old.mov",
            checksum="x",
            size=1,
            mtime=0.0,
            algo="xxhash",
            recorded_at=datetime.now(UTC),
        )
        new = VerificationSnapshotRecord(
            folder="/data",
            path="/data/new.mov",
            checksum="y",
            size=2,
            mtime=0.0,
            algo="xxhash",
            recorded_at=datetime.now(UTC),
        )
        store.replace_verification_snapshot("/data", [old])
        store.replace_verification_snapshot("/data", [new])
        loaded = store.get_verification_snapshot("/data")
        assert len(loaded) == 1
        assert loaded[0].path == "/data/new.mov"


# ---------------------------------------------------------------------------
# Empty-baseline regression test (review: blind spot where trust is needed)
# ---------------------------------------------------------------------------


class TestEmptyBaseline:
    """The review found that verifying an empty folder wrote no baseline marker,
    so a file added afterward was silently absorbed as a first-run baseline.
    The verification_baselines table now records is_empty independently of
    snapshot rows, so added files are detected."""

    def test_empty_then_add_reports_added(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "bkup"
        folder.mkdir()
        store = _make_store(store_dir)

        # First run on an EMPTY folder — establishes an empty baseline.
        first = verify_folder(folder, store)
        assert first.exit_code == 0
        assert first.is_clean is True

        # A file appears later (the exact review scenario).
        (folder / "clip.mov").write_bytes(b"arrived after baseline")

        # Must be reported as ADDED, not silently re-baselined as clean.
        second = verify_folder(folder, store)
        assert second.files_added == 1
        assert second.exit_code == 3
        assert second.is_clean is False

    def test_empty_baseline_is_persisted(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "bkup"
        folder.mkdir()
        store = _make_store(store_dir)
        verify_folder(folder, store)

        baseline = store.get_verification_baseline(str(folder.resolve()))
        assert baseline is not None
        is_empty, algo = baseline
        assert is_empty is True
        assert algo == "xxhash"

    def test_nonempty_baseline_marks_not_empty(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "bkup"
        folder.mkdir()
        (folder / "a").write_bytes(b"x")
        store = _make_store(store_dir)
        verify_folder(folder, store)

        baseline = store.get_verification_baseline(str(folder.resolve()))
        assert baseline is not None
        assert baseline[0] is False  # had files at baseline


# ---------------------------------------------------------------------------
# Audit provenance regression test (review: config_hash never supplied)
# ---------------------------------------------------------------------------


class TestConfigHashProvenance:
    """The review found runs.config_hash existed in the schema but every
    capability called start_run() without it, so results could not be tied to
    the config that produced them. verify_folder must now record a hash."""

    def test_run_records_config_hash(self, tmp_path: Path, store_dir: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "a").write_bytes(b"hello")
        store = _make_store(store_dir)

        cfg = MediaMateConfig(checksum_algo=ChecksumAlgo.SHA256)
        verify_folder(folder, store, config=cfg)

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute("SELECT config_hash FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row[0] is not None and row[0] != ""

    def test_different_configs_yield_different_hashes(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        a = MediaMateConfig(checksum_algo=ChecksumAlgo.XXHASH)
        b = MediaMateConfig(checksum_algo=ChecksumAlgo.SHA256)
        assert a.config_hash() != b.config_hash()
