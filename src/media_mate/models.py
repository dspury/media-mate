"""Pydantic models for media-mate.

These models define the shape of data flowing through media-mate:
- Input/output schemas for each capability (probe, organize, proxy, resolve, verify)
- Schema for the on-disk config file (media-mate.toml)
- Persistence models that mirror the SQLite audit log tables (see log.py)

All models use pydantic v2 and are JSON-serializable out of the box.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):  # noqa: UP042
    """Status of a media-mate run."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class ChecksumAlgo(str, Enum):  # noqa: UP042
    """Checksum algorithm used for backup verification."""

    XXHASH = "xxhash"
    SHA256 = "sha256"


# ---------------------------------------------------------------------------
# Probe models (output of probe capability)
# ---------------------------------------------------------------------------


class MediaProbe(BaseModel):
    """Structured metadata extracted from a single media file via ffprobe."""

    model_config = ConfigDict(frozen=True)

    path: str
    container: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    r_frame_rate: float | None = None  # real frame rate for VFR detection
    is_vfr: bool = False  # True when r_frame_rate differs meaningfully from frame_rate
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    bit_depth: int | None = None
    sample_aspect_ratio: str | None = None  # e.g. "16:9", "2:1"
    timecode: str | None = None  # e.g. "01:23:45:12"
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    audio_bit_depth: int | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    modification_time: datetime | None = None
    probed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Organize models (config + output)
# ---------------------------------------------------------------------------


class OrganizeConfig(BaseModel):
    """Top-level organize configuration.

    Template placeholders: {root}, {codec_family}, {resolution_bucket},
    {filename}, {ext}, {date}, {source_relpath}.

    Default template preserves the source folder structure under dest_root
    ({source_relpath}), which matches how AEs and DITs think about media
    (cards/scenes/takes). Use {codec_family}/{resolution_bucket} as
    an alternative layout when you want codec+resolution grouping.
    """

    template: str = "{root}/{source_relpath}/{filename}{ext}"
    on_conflict: Literal["skip", "overwrite", "rename"] = "skip"
    mode: Literal["copy", "move"] = "copy"


class OrganizeResult(BaseModel):
    """Output of running organize on a folder."""

    source_path: str
    destination_root: str
    files_moved: int
    files_skipped: int
    bytes_moved: int
    duration_seconds: float
    dry_run: bool
    errors: list[str] = Field(default_factory=list)
    span_warnings: list[str] = Field(default_factory=list)  # multi-file clip detections


class OrganizeOpRecord(BaseModel):
    """One row in the organize_ops table — a single file move during organize."""

    id: int | None = None
    run_id: int
    source_path: str
    destination_path: str
    operation: Literal["copy", "move", "link"] = "copy"  # link = hardlink (same-device)
    codec_family: str | None
    resolution_bucket: str | None
    file_size: int | None
    moved_at: datetime


# ---------------------------------------------------------------------------
# Proxy models
# ---------------------------------------------------------------------------


class ProxyRequest(BaseModel):
    """A request to generate a single proxy file."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    output_path: str
    codec: str = "ProRes422Proxy"
    target_height: int = 1080
    probe: MediaProbe | None = None  # optional probe data for correct ffmpeg flags


class ProxyResult(BaseModel):
    """Output of generating a single proxy."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    proxy_path: str
    codec: str
    width: int
    height: int
    file_size_bytes: int
    duration_seconds: float
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProxyFailure(BaseModel):
    """One file that failed (or was refused) during a proxy batch."""

    source_path: str
    reason: str


class ProxySkip(BaseModel):
    """One file that was skipped because its proxy already existed."""

    source_path: str
    proxy_path: str


class ProxyBatchResult(BaseModel):
    """Output of running proxy generation on a folder.

    skipped lists non-video files excluded from the batch (subtitles,
    sidecar databases, ...) — they are not failures.
    already_existed lists files whose proxy was already present — also not
    a failure; distinct from skipped so callers can distinguish them.
    """

    results: list[ProxyResult] = Field(default_factory=list)
    failures: list[ProxyFailure] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    already_existed: list[ProxySkip] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolve models
# ---------------------------------------------------------------------------


class ResolveProjectSpec(BaseModel):
    """Request to create a DaVinci Resolve project."""

    model_config = ConfigDict(frozen=True)

    name: str
    source_folder: str
    output_path: str
    resolution: Literal["1080", "4K", "720"] = "1080"
    frame_rate: Literal["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"] = "24"
    color_space: str = "Rec.709"


class ResolveProjectResult(BaseModel):
    """Output of Resolve project creation."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    resolution: str
    frame_rate: str
    color_space: str
    bin_count: int
    timeline_count: int
    resolve_version: str | None = None  # None when FFmpeg fallback was used
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Verify models
# ---------------------------------------------------------------------------


class VerificationReport(BaseModel):
    """Output of a backup verification run."""

    model_config = ConfigDict(frozen=True)

    folder: str
    files_checked: int
    files_missing: int
    files_modified: int
    files_added: int
    checksum_algo: ChecksumAlgo
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exit_code: int  # 0=clean, 1=missing, 2=modified, 3=added (combined if multiple)

    @property
    def is_clean(self) -> bool:
        """True if the verification found no differences."""
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Top-level config (media-mate.toml)
# ---------------------------------------------------------------------------


class MediaMateConfig(BaseModel):
    """Top-level media-mate configuration loaded from media-mate.toml."""

    model_config = ConfigDict(extra="forbid")

    organize: OrganizeConfig = Field(default_factory=OrganizeConfig)
    proxy_codec: str = "ProRes422Proxy"
    proxy_height: int = 1080
    checksum_algo: ChecksumAlgo = ChecksumAlgo.XXHASH
    resolve_path: str | None = None  # None = auto-detect
    ffmpeg_path: str | None = None  # None = auto-detect (PATH lookup)


# ---------------------------------------------------------------------------
# Persistence models (mirror SQLite audit log tables in log.py)
# ---------------------------------------------------------------------------


class RunRecord(BaseModel):
    """One row in the runs table."""

    id: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    command: str
    config_hash: str | None = None
    status: RunStatus
    error: str | None = None


class FileRecord(BaseModel):
    """One row in the files table — every file the system has ever seen."""

    id: int | None = None
    path: str
    size: int | None = None
    mtime: float | None = None
    first_seen_run: int | None = None
    last_seen_run: int | None = None


class ProbeRecord(BaseModel):
    """One row in the probes table."""

    id: int | None = None
    file_id: int
    run_id: int
    codec: str | None
    container: str | None
    width: int | None
    height: int | None
    frame_rate: float | None
    color_space: str | None
    bit_depth: int | None
    duration: float | None
    audio_channels: int | None
    audio_sample_rate: int | None
    probed_at: datetime


class ProxyRecord(BaseModel):
    """One row in the proxies table."""

    id: int | None = None
    source_file_id: int
    proxy_path: str
    run_id: int
    codec: str
    width: int
    height: int
    file_size: int
    generated_at: datetime


class ProjectRecord(BaseModel):
    """One row in the projects table."""

    id: int | None = None
    name: str
    path: str
    run_id: int
    resolution: str | None
    frame_rate: str | None
    color_space: str | None
    bin_count: int
    timeline_count: int
    resolve_version: str | None
    created_at: datetime


class VerificationRecord(BaseModel):
    """One row in the verifications table."""

    id: int | None = None
    folder: str
    run_id: int
    files_checked: int
    files_missing: int
    files_modified: int
    files_added: int
    checksum_algo: str
    verified_at: datetime


class VerificationSnapshotRecord(BaseModel):
    """One row in the verification_snapshots table — a single file's recorded checksum.

    The (folder, path) pair is unique; each folder has at most one row per file.
    The snapshot for a folder is the set of all rows for that folder.
    """

    folder: str
    path: str
    checksum: str
    size: int | None
    mtime: float | None
    algo: str
    recorded_at: datetime


__all__ = [
    "ChecksumAlgo",
    "FileRecord",
    "MediaMateConfig",
    "MediaProbe",
    "OrganizeConfig",
    "OrganizeOpRecord",
    "OrganizeResult",
    "ProbeRecord",
    "ProjectRecord",
    "ProxyBatchResult",
    "ProxyFailure",
    "ProxyRecord",
    "ProxyRequest",
    "ProxyResult",
    "ProxySkip",
    "ResolveProjectResult",
    "ResolveProjectSpec",
    "RunRecord",
    "RunStatus",
    "VerificationRecord",
    "VerificationReport",
    "VerificationSnapshotRecord",
]


# Silence unused-import warning for Path/Path-style imports — kept for future schema work.
_ = Path
