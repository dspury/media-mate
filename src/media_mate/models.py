"""Pydantic models for media-mate.

These models define the shape of data flowing through media-mate:
- Input/output schemas for each capability (probe, organize, proxy, resolve, verify)
- Schema for the on-disk config file (media-mate.toml)
- Persistence models that mirror the SQLite audit log tables (see log.py)

All models use pydantic v2 and are JSON-serializable out of the box.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, Enum):
    """Status of a media-mate run."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class ChecksumAlgo(str, Enum):
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
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    bit_depth: int | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    audio_bit_depth: int | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    modification_time: datetime | None = None
    probed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Organize models (config + output)
# ---------------------------------------------------------------------------


class OrganizeRule(BaseModel):
    """A single organize rule: maps a folder-template to a codec family/resolution bucket.

    Templates use Python str.format placeholders:
        {root}, {codec_family}, {resolution_bucket}, {filename}, {ext}, {date}
    """

    model_config = ConfigDict(frozen=True)

    codec_family: str
    resolution_bucket: str
    template: str = "{root}/{codec_family}/{resolution_bucket}/{filename}{ext}"


class OrganizeConfig(BaseModel):
    """Top-level organize configuration."""

    rules: list[OrganizeRule] = Field(default_factory=list)
    default_template: str = "{root}/{codec_family}/{resolution_bucket}/{filename}{ext}"


class OrganizeResult(BaseModel):
    """Output of running organize on a folder."""

    source_path: str
    destination_path: str
    files_moved: int
    bytes_moved: int
    duration_seconds: float


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
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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


__all__ = [
    "ChecksumAlgo",
    "FileRecord",
    "MediaMateConfig",
    "MediaProbe",
    "OrganizeConfig",
    "OrganizeResult",
    "OrganizeRule",
    "ProbeRecord",
    "ProjectRecord",
    "ProxyRecord",
    "ProxyRequest",
    "ProxyResult",
    "ResolveProjectResult",
    "ResolveProjectSpec",
    "RunRecord",
    "RunStatus",
    "VerificationRecord",
    "VerificationReport",
]


# Silence unused-import warning for Path/Path-style imports — kept for future schema work.
_ = Path
