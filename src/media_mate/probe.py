"""Probe capability — extract structured metadata from media files via ffprobe.

Public API:
    find_ffprobe(config) -> str
        Locate the ffprobe binary; raise ProbeError if not found.
    probe_file(path, ffprobe_path=None) -> MediaProbe
        Probe a single file via ffprobe.
    probe_path(path, store, config=None) -> list[MediaProbe]
        Probe a file or recursively a directory; write all results to the audit log.

Errors are surfaced as ProbeError so callers can decide whether to fail-fast or
continue-on-error (probe_path takes the continue-on-error approach).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from media_mate.log import LogStore
from media_mate.models import MediaMateConfig, MediaProbe, ProbeRecord, RunStatus


class ProbeError(Exception):
    """Raised when probing fails for a specific reason."""

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"Failed to probe {path}: {reason}")
        self.path = Path(path) if not isinstance(path, Path) else path
        self.reason = reason


def find_ffprobe(config: MediaMateConfig | None = None) -> str:
    """Locate the ffprobe binary.

    Resolution order:
    1. config.ffmpeg_path — derive ffprobe from the same directory
    2. ``ffprobe`` on PATH
    3. ``ffmpeg`` on PATH — derive ffprobe from the same directory

    Raises ProbeError if no ffprobe binary can be located.
    """
    candidates: list[Path | str] = []

    if config and config.ffmpeg_path:
        candidates.append(Path(config.ffmpeg_path).with_name("ffprobe"))
        candidates.append(config.ffmpeg_path)

    candidates.append("ffprobe")

    for c in candidates:
        if isinstance(c, Path):
            if c.is_file():
                return str(c)
        else:
            resolved = shutil.which(c)
            if resolved:
                return resolved

    raise ProbeError("ffprobe", "binary not found on PATH and config.ffmpeg_path not set")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    """Parse a value as float; return None on failure or N/A."""
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    """Parse a value as int; return None on failure or N/A."""
    if value is None or value == "" or value == "N/A":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_frame_rate(rate: str | None) -> float | None:
    """Parse an ffprobe frame-rate string like '24/1' or '30000/1001' to a float.

    ffprobe reports frame rates as fractions. 30000/1001 ≈ 29.97 (NTSC).
    """
    if not rate or rate == "0/0":
        return None
    try:
        if "/" in rate:
            num_s, den_s = rate.split("/", 1)
            num = float(num_s)
            den = float(den_s)
            if den == 0:
                return None
            return num / den
        return float(rate)
    except (TypeError, ValueError):
        return None


_BIT_DEPTH_FROM_PIX_FMT: dict[str, int] = {
    # 8-bit
    "yuv420p": 8,
    "yuv422p": 8,
    "yuv444p": 8,
    "yuv410p": 8,
    "yuv411p": 8,
    "yuvj420p": 8,
    "yuvj422p": 8,
    "yuvj444p": 8,
    "rgb24": 8,
    "bgr24": 8,
    "gbrp": 8,
    # 10-bit
    "yuv420p10le": 10,
    "yuv422p10le": 10,
    "yuv444p10le": 10,
    "yuv420p10be": 10,
    "yuv422p10be": 10,
    "yuv444p10be": 10,
    "yuv420p12le": 12,
    "yuv422p12le": 12,
    "yuv444p12le": 12,
    "rgb48be": 16,
    "rgb48le": 16,
    "bgr48be": 16,
    "bgr48le": 16,
}


def _bit_depth_from_pix_fmt(pix_fmt: str | None) -> int | None:
    """Derive bit depth from a pix_fmt string like 'yuv420p10le'."""
    if not pix_fmt:
        return None
    return _BIT_DEPTH_FROM_PIX_FMT.get(pix_fmt.lower())


def _is_vfr(avg: float | None, rfr: float | None) -> bool:
    """Return True when r_frame_rate differs from avg_frame_rate by > 1 percent."""
    if avg is None or rfr is None or avg == 0:
        return False
    return abs(rfr - avg) / avg > 0.01


def _extract_timecode(raw: dict[str, Any]) -> str | None:
    """Extract timecode from parsed ffprobe JSON.

    Checks:
    - format.tags.timecode (or TIMEcode)
    - video stream disposition.timecode
    """
    tags = (raw.get("format") or {}).get("tags") or {}
    tc = tags.get("timecode") or tags.get("TIMEcode")
    if tc:
        return tc
    for stream in raw.get("streams") or []:
        if stream.get("codec_type") == "video":
            tc = stream.get("disposition", {}).get("timecode")
            if tc:
                return tc
    return None


def _parse_ffprobe_output(path: Path, raw: dict[str, Any]) -> MediaProbe:
    """Map ffprobe's JSON output to a MediaProbe."""
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    try:
        stat = path.stat()
        file_size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    except OSError:
        file_size = None
        mtime = None

    # ffprobe may report size in the format dict or not at all
    fmt_size = _safe_int(fmt.get("size"))
    if fmt_size is not None:
        file_size = fmt_size

    # Frame rates
    avg_frame_rate = _parse_frame_rate(video.get("avg_frame_rate")) if video else None
    r_frame_rate = _parse_frame_rate(video.get("r_frame_rate")) if video else None
    is_vfr = _is_vfr(avg_frame_rate, r_frame_rate)

    # Bit depth: prefer bits_per_raw_sample; fall back to pix_fmt parsing
    bit_depth: int | None = None
    if video:
        raw_bits = _safe_int(video.get("bits_per_raw_sample"))
        if raw_bits:
            bit_depth = raw_bits
        else:
            bit_depth = _bit_depth_from_pix_fmt(video.get("pix_fmt"))

    return MediaProbe(
        path=str(path),
        container=fmt.get("format_name"),
        video_codec=video.get("codec_name") if video else None,
        width=_safe_int(video.get("width")) if video else None,
        height=_safe_int(video.get("height")) if video else None,
        frame_rate=avg_frame_rate,
        r_frame_rate=r_frame_rate,
        is_vfr=is_vfr,
        color_space=video.get("color_space") if video else None,
        color_transfer=video.get("color_transfer") if video else None,
        color_primaries=video.get("color_primaries") if video else None,
        bit_depth=bit_depth,
        sample_aspect_ratio=video.get("sample_aspect_ratio") if video else None,
        timecode=_extract_timecode(raw),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=_safe_int(audio.get("channels")) if audio else None,
        audio_sample_rate=_safe_int(audio.get("sample_rate")) if audio else None,
        audio_bit_depth=_safe_int(audio.get("bits_per_sample")) if audio else None,
        duration_seconds=_safe_float(fmt.get("duration")),
        file_size_bytes=file_size,
        modification_time=mtime,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def probe_file(path: Path, ffprobe_path: str | None = None) -> MediaProbe:
    """Probe a single file via ffprobe. Returns a MediaProbe.

    Raises ProbeError if the path doesn't exist, ffprobe isn't found, ffprobe
    returns an error, or ffprobe's output isn't valid JSON.
    """
    path = Path(path)
    if not path.is_file():
        raise ProbeError(path, "not a file or does not exist")

    fp = ffprobe_path or find_ffprobe()

    cmd = [
        fp,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise ProbeError(path, f"ffprobe not found at {fp}: {e}") from e

    if result.returncode != 0:
        raise ProbeError(path, f"ffprobe exited {result.returncode}: {result.stderr.strip()}")

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(path, f"ffprobe returned invalid JSON: {e}") from e

    return _parse_ffprobe_output(path, raw)


def probe_path(
    path: Path,
    store: LogStore,
    config: MediaMateConfig | None = None,
) -> list[MediaProbe]:
    """Probe a file or recursively a directory; write results to the audit log.

    Returns the list of successful MediaProbe results. Files that fail to probe
    are skipped (with their errors recorded in the run's error field) rather
    than aborting the whole batch.

    Run status:
        - SUCCESS: all files probed successfully
        - PARTIAL: some succeeded, some failed
        - FAILED: no files succeeded
    """
    path = Path(path)

    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
    else:
        raise ProbeError(path, "not a file or directory")

    if not files:
        return []

    ffprobe_path = find_ffprobe(config)
    command = f"media-mate probe {path}"
    run_id = store.start_run(command)

    results: list[MediaProbe] = []
    errors: list[tuple[Path, str]] = []

    for f in files:
        try:
            probe = probe_file(f, ffprobe_path)
            results.append(probe)

            file_id = store.upsert_file(
                str(f),
                size=probe.file_size_bytes,
                mtime=f.stat().st_mtime if f.exists() else None,
                run_id=run_id,
            )
            store.insert_probe(
                ProbeRecord(
                    file_id=file_id,
                    run_id=run_id,
                    codec=probe.video_codec or probe.audio_codec,
                    container=probe.container,
                    width=probe.width,
                    height=probe.height,
                    frame_rate=probe.frame_rate,
                    color_space=probe.color_space,
                    bit_depth=probe.bit_depth,
                    duration=probe.duration_seconds,
                    audio_channels=probe.audio_channels,
                    audio_sample_rate=probe.audio_sample_rate,
                    probed_at=probe.probed_at,
                )
            )
        except ProbeError as e:
            errors.append((e.path, e.reason))

    if not errors:
        status = RunStatus.SUCCESS
        error_msg = None
    elif results:
        status = RunStatus.PARTIAL
        error_msg = _format_errors(errors)
    else:
        status = RunStatus.FAILED
        error_msg = _format_errors(errors)

    store.finish_run(run_id, status, error_msg)
    return results


def _format_errors(errors: list[tuple[Path, str]], limit: int = 5) -> str:
    """Format an error summary for the run log."""
    head = "; ".join(f"{p.name}: {r}" for p, r in errors[:limit])
    if len(errors) > limit:
        head += f"; ... ({len(errors) - limit} more)"
    return f"{len(errors)} file(s) failed: {head}"


__all__ = [
    "ProbeError",
    "find_ffprobe",
    "probe_file",
    "probe_path",
]
