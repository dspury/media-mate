"""Verify capability — backup integrity verification via checksums.

Public API:
    compute_checksum(path, algo="xxhash") -> str
        Compute a checksum for a single file. Streams in 64KB chunks so it's
        safe for large media files.
    verify_folder(folder, store, config=None, accept_changes=False) -> VerificationReport
        Compute current checksums for a folder, diff against the previously
        recorded snapshot. Returns a structured report.

Workflow:
    First call: snapshot is created (no prior baseline), report shows 0 diffs.
    Subsequent calls: diff against the previous snapshot. The baseline is
    NOT updated on mismatch — use --accept-changes to explicitly set a new
    baseline after reviewing the diff. Designed for cron.

Exit codes (per SPEC.md §5.5; priority-ordered):
    0 = clean (no diffs, or first-time snapshot)
    1 = missing (one or more files in previous snapshot no longer exist)
    2 = modified (checksums differ from previous snapshot)
    3 = added (files present but not in previous snapshot)

When multiple categories of change are present, the highest-priority category
determines the exit code (missing > modified > added). The report fields
(files_missing, files_modified, files_added) carry the full count for each.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import xxhash

from media_mate.log import LogStore
from media_mate.models import (
    ChecksumAlgo,
    MediaMateConfig,
    RunStatus,
    VerificationRecord,
    VerificationReport,
    VerificationSnapshotRecord,
)
from media_mate.probe import is_system_artifact

_CHUNK_SIZE = 65536  # 64 KB


class VerifyError(Exception):
    """Raised when verify cannot proceed."""


# ---------------------------------------------------------------------------
# Checksum primitives
# ---------------------------------------------------------------------------


def _hash_for(algo: ChecksumAlgo) -> xxhash.xxh64 | hashlib._Hash:
    """Return a fresh hasher for the given algorithm."""
    if algo == ChecksumAlgo.XXHASH:
        return xxhash.xxh64()
    if algo == ChecksumAlgo.SHA256:
        return hashlib.sha256()
    raise VerifyError(f"unsupported checksum algorithm: {algo}")


def compute_checksum(path: Path, algo: ChecksumAlgo = ChecksumAlgo.XXHASH) -> str:
    """Compute a hex checksum for a single file.

    Streams the file in 64KB chunks; safe for multi-GB media files.

    Raises VerifyError if the path is not a file.
    """
    path = Path(path)
    if not path.is_file():
        raise VerifyError(f"not a file: {path}")

    hasher = _hash_for(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK_SIZE), b""):
            hasher.update(chunk)
    digest: str = hasher.hexdigest()
    return digest


def _iter_files(folder: Path) -> Iterator[Path]:
    """Yield all files under folder, recursively, sorted by path.

    System artifacts (.DS_Store, AppleDouble ._* sidecars, $RECYCLE.BIN, …)
    are excluded: they churn constantly and would otherwise raise false
    added/modified alarms on every verify of a mounted card or backup drive.
    """
    yield from sorted(
        p for p in folder.rglob("*") if p.is_file() and not is_system_artifact(p, folder)
    )


# ---------------------------------------------------------------------------
# Snapshot helpers (called from verify_folder; exposed for tests)
# ---------------------------------------------------------------------------


def _snapshot_records(
    folder: Path, algo: ChecksumAlgo, at: datetime
) -> list[VerificationSnapshotRecord]:
    """Compute checksums for all files in folder; return snapshot records (unsaved)."""
    folder_str = str(folder)
    return [
        VerificationSnapshotRecord(
            folder=folder_str,
            path=str(f),
            checksum=compute_checksum(f, algo),
            size=f.stat().st_size,
            mtime=f.stat().st_mtime,
            algo=algo.value,
            recorded_at=at,
        )
        for f in _iter_files(folder)
    ]


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def verify_folder(
    folder: Path,
    store: LogStore,
    config: MediaMateConfig | None = None,
    *,
    accept_changes: bool = False,
) -> VerificationReport:
    """Verify a folder against the previous snapshot; write a new snapshot.

    First call for a folder: creates a snapshot, returns a clean report.
    Subsequent calls: diff against the previous snapshot.

    The baseline is NOT updated on mismatch (immutable baseline). This prevents
    a corrupted file from silently overwriting the known-good baseline.
    Use --accept-changes (accept_changes=True) to explicitly acknowledge
    a new baseline after reviewing the diff.

    The verification itself is logged to the runs + verifications tables
    in the audit log, so the run history is queryable.

    Raises VerifyError if folder is not a directory.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise VerifyError(f"not a directory: {folder}")

    cfg = config or MediaMateConfig()
    algo = cfg.checksum_algo
    now = datetime.now(UTC)
    folder_str = str(folder)

    # Get previous baseline and snapshot
    baseline = store.get_verification_baseline(folder_str)
    prev_rows = store.get_verification_snapshot(folder_str)
    prev: dict[str, str] = {row.path: row.checksum for row in prev_rows}

    # Detect algo mismatch with existing baseline
    if baseline:
        existing_algo = baseline[1]
        if existing_algo != algo.value:
            raise VerifyError(
                f"existing baseline uses '{existing_algo}' but config says "
                f"'{algo.value}'; clear the snapshot or set config.checksum_algo "
                f"to '{existing_algo}'"
            )

    # Compute current snapshot
    new_rows = _snapshot_records(folder, algo, now)
    new: dict[str, str] = {row.path: row.checksum for row in new_rows}

    is_first_run = baseline is None
    baseline_was_empty = baseline is not None and baseline[0]

    if is_first_run:
        # No prior baseline — first verify establishes the baseline; report is clean.
        missing: list[str] = []
        added: list[str] = []
        modified: list[str] = []
    else:
        prev_paths = set(prev.keys())
        new_paths = set(new.keys())
        missing = sorted(prev_paths - new_paths)
        added = sorted(new_paths - prev_paths)
        common = prev_paths & new_paths
        modified = sorted(p for p in common if prev[p] != new[p])

        # If the baseline was recorded on an empty folder, any files now present
        # are genuine additions — not a quirk of snapshot rows not existing.
        if baseline_was_empty and added:
            pass  # added files are real; no correction needed
        # Backwards-compat: if prev_rows is empty but baseline exists (shouldn't
        # happen with atomic writes, but guard against it), treat all new as added.
        elif not prev_rows and baseline and not baseline_was_empty:
            added = sorted(new_paths)

    # Persist the new snapshot only when verification is clean, or when
    # the user explicitly acknowledges the new baseline via --accept-changes.
    # On mismatch without acceptance, the baseline is immutable — corruption
    # does not suppress future detections by overwriting the known-good snapshot.
    is_empty_folder = len(new_rows) == 0
    exit_code = _exit_code(missing=bool(missing), modified=bool(modified), added=bool(added))
    if exit_code == 0 or accept_changes:
        store.replace_verification_snapshot(
            folder_str, new_rows, is_empty=is_empty_folder, algo=algo.value
        )

    # Log the run
    command = f"media-mate verify {folder}"
    run_id = store.start_run(command, config_hash=cfg.config_hash())
    store.insert_verification(
        VerificationRecord(
            folder=folder_str,
            run_id=run_id,
            files_checked=len(new),
            files_missing=len(missing),
            files_modified=len(modified),
            files_added=len(added),
            checksum_algo=algo.value,
            verified_at=now,
        )
    )
    # The verification itself succeeded; the diff content is in the report fields.
    store.finish_run(run_id, RunStatus.SUCCESS)

    return VerificationReport(
        folder=folder_str,
        files_checked=len(new),
        files_missing=len(missing),
        files_modified=len(modified),
        files_added=len(added),
        checksum_algo=algo,
        verified_at=now,
        exit_code=_exit_code(missing=bool(missing), modified=bool(modified), added=bool(added)),
    )


def _exit_code(*, missing: bool, modified: bool, added: bool) -> int:
    """Priority-ordered exit code per SPEC §5.5: 0=clean, 1=missing, 2=modified, 3=added.

    Higher-priority categories win. So missing+modified reports as 1, not 2;
    the report's individual count fields carry the full breakdown.
    """
    if missing:
        return 1
    if modified:
        return 2
    if added:
        return 3
    return 0


__all__ = [
    "VerifyError",
    "compute_checksum",
    "verify_folder",
]
