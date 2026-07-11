"""Organize capability — auto-arrange media files into a structured folder layout.

Public API:
    codec_family(codec) -> str
        Classify a codec name into a family bucket.
    resolution_bucket(height) -> str
        Classify a video height into a resolution bucket.
    build_destination_path(template, dest_root, source, family, bucket) -> Path
        Render an organize template to a destination Path.
    organize_path(source, dest_root, store, config=None, dry_run=False) -> OrganizeResult
        Move (or dry-run) files from source into dest_root based on probe data.

Files without probe data in the audit log are skipped (run `media-mate probe`
first to populate). Every move is recorded in the organize_ops table so the
operation can be reversed in a future release.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from media_mate.log import LogStore
from media_mate.models import (
    MediaMateConfig,
    OrganizeOpRecord,
    OrganizeResult,
    RunStatus,
)


class OrganizeError(Exception):
    """Raised when organize cannot proceed (e.g., bad source path)."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


_CODEC_FAMILY_MAP: dict[str, str] = {
    # ProRes (ffprobe reports all ProRes variants as "prores")
    "prores": "prores",
    # H.264 / AVC
    "h264": "h264",
    "avc": "h264",
    "avc1": "h264",
    "avc3": "h264",
    # H.265 / HEVC
    "h265": "h265",
    "hevc": "h265",
    "hvc1": "h265",
    "hev1": "h265",
    # MPEG family
    "mpeg2video": "mpeg",
    "mpeg4": "mpeg",
    "mjpeg": "mpeg",
    # Modern codecs
    "vp9": "modern",
    "av1": "modern",
    "theora": "modern",
    # DNx family
    "dnxhd": "dnx",
    "dnxhr": "dnx",
    # RAW camera formats
    "rawvideo": "raw",
    # Audio
    "mp3": "audio",
    "aac": "audio",
    "flac": "audio",
    "opus": "audio",
    "vorbis": "audio",
    "alac": "audio",
    "pcm_s16le": "audio",
    "pcm_s24le": "audio",
    "pcm_s32le": "audio",
    "pcm_f32le": "audio",
}

# Patterns that suggest a multi-file / spanned clip:
# e.g. ClipName_001.mov, ClipName_002.mxf, ClipName.RDC
_SPANNED_PATTERNS = (
    r"^(?P<base>.+?)[\._](?P<seq>\d{3,6})\.[a-zA-Z0-9]+$",
    r"^(?P<base>.+?)\.RDC$",
)


def _spanned_clip_groups(
    files: list[Path],
) -> list[tuple[str, list[Path]]]:
    """Detect groups of multi-file / spanned clips and return (base_name, parts).

    Returns a list of (base_name, [paths...]) where each group has 2+ parts.
    Checks for sequential numeric suffixes (_001, _002, ...) and RED .RDC files.
    """
    groups: dict[str, list[Path]] = {}
    for f in files:
        name = f.name
        for pat in _SPANNED_PATTERNS:
            m = re.match(pat, name, re.IGNORECASE)
            if m:
                base = m.group("base")
                groups.setdefault(base, []).append(f)
                break

    return [(base, parts) for base, parts in groups.items() if len(parts) >= 2]


def codec_family(codec: str | None) -> str:
    """Map a codec name to a coarse family bucket.

    None -> "unknown". Recognized codecs -> family name. Unrecognized codecs
    -> the lowercase codec name itself (preserves info for debugging).
    """
    if codec is None:
        return "unknown"
    return _CODEC_FAMILY_MAP.get(codec.lower(), codec.lower())


def resolution_bucket(height: int | None) -> str:
    """Map a video height to a coarse resolution bucket."""
    if height is None or height <= 0:
        return "unknown"
    if height <= 480:
        return "480p"
    if height <= 720:
        return "720p"
    if height <= 1080:
        return "1080p"
    if height <= 1440:
        return "1440p"
    if height <= 2160:
        return "4K"
    if height <= 4320:
        return "8K"
    return f"{height}p"


# ---------------------------------------------------------------------------
# Path building
# ---------------------------------------------------------------------------


def build_destination_path(
    template: str,
    dest_root: Path,
    source: Path,
    family: str,
    bucket: str,
    date: str | None = None,
    source_root: Path | None = None,
) -> Path:
    """Render an organize template to a destination Path.

    Template placeholders: {root}, {codec_family}, {resolution_bucket},
    {filename}, {ext}, {date}, {source_relpath}.

    source_relpath is the directory part of the source file relative to
    source_root (i.e., source's parent directory relative to source_root),
    preserving any subfolder structure under the organize root.
    """
    source_relpath = ""
    if source_root is not None:
        try:
            # source is the file path; source_relpath is its directory
            # relative to source_root (preserves card/scene subfolders)
            source_relpath = str(source.parent.relative_to(source_root))
        except ValueError:
            # source is not under source_root
            source_relpath = ""

    ctx = {
        "root": str(dest_root),
        "codec_family": family,
        "resolution_bucket": bucket,
        "filename": source.stem,
        "ext": source.suffix,
        "date": date or datetime.now(UTC).strftime("%Y-%m-%d"),
        "source_relpath": source_relpath,
    }
    return Path(template.format(**ctx))


def _unique_path(dest: Path) -> Path:
    """Return a non-colliding variant of dest by appending -1, -2, etc."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def organize_path(
    source: Path,
    dest_root: Path,
    store: LogStore,
    config: MediaMateConfig | None = None,
    dry_run: bool = False,
    move: bool | None = None,
) -> OrganizeResult:
    """Organize files from source into dest_root.

    Files are classified by codec family and resolution bucket using probe
    data already in the audit log. Files without probe data are skipped
    (with the reason recorded in errors); the user should run probe first.

    Source files are copied by default so the raw folder stays intact —
    treat originals as immutable camera media. Pass move=True (or set
    [organize] mode = "move" in config) to relocate instead. When move is
    None the config decides.

    Each operation is recorded in organize_ops so it can be reversed in a
    future release.

    Run status:
        - SUCCESS: all probed files organized successfully
        - PARTIAL: some organized, some skipped (no probe / conflict / OSError)
        - FAILED: nothing organized
    """
    source = Path(source)
    dest_root = Path(dest_root)
    cfg = config or MediaMateConfig()
    organize_cfg = cfg.organize
    do_move = move if move is not None else organize_cfg.mode == "move"
    started = datetime.now(UTC)

    if not source.exists():
        raise OrganizeError(f"source path does not exist: {source}")
    if not source.is_dir():
        raise OrganizeError(f"source is not a directory: {source}")

    files = sorted(p for p in source.rglob("*") if p.is_file())

    # Detect multi-file / spanned clips before organizing (logged as warnings)
    span_warnings: list[str] = []
    spanned = _spanned_clip_groups(files)
    for base, parts in spanned:
        span_warnings.append(
            f"[SPAN] {base}: {len(parts)} files detected as multi-file clip "
            f"({', '.join(p.name for p in parts)}); "
            f"organizing individually — verify all parts are included"
        )

    if not files:
        return OrganizeResult(
            source_path=str(source),
            destination_root=str(dest_root),
            files_moved=0,
            files_skipped=0,
            bytes_moved=0,
            duration_seconds=(datetime.now(UTC) - started).total_seconds(),
            dry_run=dry_run,
            errors=[],
        )

    # Look up probe data for all files in one query
    probes = store.get_latest_probes_by_paths([str(f) for f in files])

    command = f"media-mate organize {source} --root {dest_root}"
    if do_move:
        command += " --move"
    run_id = store.start_run(command, config_hash=cfg.config_hash())

    files_moved = 0
    files_skipped = 0
    bytes_moved = 0
    errors: list[str] = []

    for f in files:
        try:
            size = f.stat().st_size  # capture before copy/move

            probe = probes.get(str(f))
            if probe is None:
                files_skipped += 1
                errors.append(f"{f.name}: no probe data — run `media-mate probe` first")
                continue

            family = codec_family(probe.codec)
            bucket = resolution_bucket(probe.height)
            dest = build_destination_path(
                organize_cfg.template, dest_root, f, family, bucket, source_root=source
            )

            # Conflict handling
            if dest.exists():
                if organize_cfg.on_conflict == "skip":
                    files_skipped += 1
                    errors.append(f"{f.name}: destination already exists, skipping")
                    continue
                if organize_cfg.on_conflict == "rename":
                    dest = _unique_path(dest)
                # "overwrite" falls through; copy2/move both overwrite

            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                op: Literal["copy", "move"]
                if do_move:
                    shutil.move(str(f), str(dest))
                    op = "move"
                else:
                    # Always copy. Hardlinks were considered for same-device I/O
                    # but were rejected: os.link() makes the destination an alias
                    # to the source inode, so editing the "copy" corrupts the raw.
                    # We choose correctness over the marginal speed of a hardlink.
                    shutil.copy2(str(f), str(dest))
                    op = "copy"

                store.insert_organize_op(
                    OrganizeOpRecord(
                        run_id=run_id,
                        source_path=str(f),
                        destination_path=str(dest),
                        operation=op,
                        codec_family=family,
                        resolution_bucket=bucket,
                        file_size=size,
                        moved_at=datetime.now(UTC),
                    )
                )

            files_moved += 1
            bytes_moved += size

        except OSError as e:
            files_skipped += 1
            errors.append(f"{f.name}: {e}")

    # Determine run status
    if files_moved == 0 and files_skipped > 0:
        status = RunStatus.FAILED
    elif files_moved > 0 and files_skipped > 0:
        status = RunStatus.PARTIAL
    else:
        status = RunStatus.SUCCESS

    error_summary: str | None = None
    if errors:
        head = "; ".join(errors[:5])
        if len(errors) > 5:
            head += f"; ... ({len(errors) - 5} more)"
        error_summary = f"{len(errors)} file(s) skipped: {head}"
    store.finish_run(run_id, status, error_summary)

    duration = (datetime.now(UTC) - started).total_seconds()
    return OrganizeResult(
        source_path=str(source),
        destination_root=str(dest_root),
        files_moved=files_moved,
        files_skipped=files_skipped,
        bytes_moved=bytes_moved,
        duration_seconds=duration,
        dry_run=dry_run,
        errors=errors,
        span_warnings=span_warnings,
    )


__all__ = [
    "OrganizeError",
    "_spanned_clip_groups",
    "build_destination_path",
    "codec_family",
    "organize_path",
    "resolution_bucket",
]
