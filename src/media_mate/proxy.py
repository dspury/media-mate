"""Proxy capability — generate edit-friendly proxy files via ffmpeg.

Public API:
    find_ffmpeg(config) -> str
        Locate the ffmpeg binary; raise ProxyError if not found.
    generate_proxy(request, ffmpeg_path=None) -> ProxyResult
        Generate a single proxy file. Returns ProxyResult.
    generate_proxies(source, output_dir, store, config=None) -> list[ProxyResult]
        Generate proxies for all files in source (file or directory).
        Writes audit-log rows; failed files don't abort the batch.

Default codec: ProRes 422 Proxy at 1080p height (aspect-preserving).
Audio: PCM s16le (preserves quality for editing).
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from media_mate.log import LogStore
from media_mate.models import (
    MediaMateConfig,
    MediaProbe,
    ProxyBatchResult,
    ProxyFailure,
    ProxyRecord,
    ProxyRequest,
    ProxyResult,
    ProxySkip,
    RunStatus,
)


class ProxyError(Exception):
    """Raised when proxy generation fails for a specific reason."""

    def __init__(self, path: Path | str, reason: str) -> None:
        super().__init__(f"Failed to generate proxy for {path}: {reason}")
        self.path = Path(path) if not isinstance(path, Path) else path
        self.reason = reason


# Maps codec name (case-insensitive) to ffmpeg prores_ks profile number.
# 0=proxy, 1=LT, 2=422, 3=422HQ, 4=4444, 5=4444XQ
_PRORES_PROFILE_MAP: dict[str, int] = {
    "prores422proxy": 0,
    "prores422lt": 1,
    "prores422": 2,
    "prores422hq": 3,
    "prores4444": 4,
    "prores4444xq": 5,
}


def find_ffmpeg(config: MediaMateConfig | None = None) -> str:
    """Locate the ffmpeg binary.

    Resolution order:
    1. config.ffmpeg_path (if set and points to a file)
    2. shutil.which("ffmpeg")

    Raises ProxyError if no ffmpeg binary can be located.
    """
    if config and config.ffmpeg_path:
        p = Path(config.ffmpeg_path)
        if p.is_file():
            return str(p)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise ProxyError("ffmpeg", "binary not found on PATH and config.ffmpeg_path not set")


def _profile_for(codec: str) -> int:
    """Return the ffmpeg prores_ks profile number for a ProRes codec name.

    Raises ProxyError if codec is not recognized.
    """
    profile = _PRORES_PROFILE_MAP.get(codec.lower())
    if profile is None:
        raise ProxyError(
            codec, f"unsupported codec '{codec}' — supported: {sorted(_PRORES_PROFILE_MAP)}"
        )
    return profile


def _audio_codec_for(probe: MediaProbe | None) -> str:
    """Pick the right PCM codec for audio bit depth.

    Uses the source audio bit depth when available (from probe), otherwise
    falls back to pcm_s16le as the safe default.
    """
    if probe and probe.audio_bit_depth:
        depth = probe.audio_bit_depth
        if depth >= 24:
            return "pcm_s32le"
        if depth >= 16:
            return "pcm_s16le"
    return "pcm_s16le"


def _ffmpeg_cmd(
    ffmpeg_path: str,
    source: Path,
    output: Path,
    codec: str,
    target_height: int,
    probe: MediaProbe | None = None,
) -> list[str]:
    """Build the ffmpeg command for proxy generation.

    Uses probe data to:
    - Map all audio tracks (not just the first)
    - Preserve timecode via -timecode
    - Pass through color metadata (color_space, color_transfer, color_primaries)
    - Set SAR from source to handle anamorphic footage correctly
    - Pick the right PCM bit depth from source audio
    """
    profile = _profile_for(codec)

    cmd: list[str] = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source),
    ]

    # Timecode — use probe data if available
    timecode = probe.timecode if probe else None
    if timecode:
        cmd += ["-timecode", timecode]

    # Build video filter chain
    filters: list[str] = []

    # Scale to target height, preserving aspect ratio.
    # scale=-2:H forces width to be even (required for many codecs)
    # and accepts any source SAR — we add setdar after scale to
    # restore the correct display aspect ratio.
    filters.append(f"scale=-2:{target_height}")

    # Restore SAR from source if it differs from 1:1 (anamorphic footage)
    if probe and probe.sample_aspect_ratio and probe.sample_aspect_ratio != "1:1":
        sar = probe.sample_aspect_ratio
        filters.append(f"setsar={sar}")

    cmd += ["-vf", ",".join(filters)]

    # Video codec
    cmd += ["-c:v", "prores_ks", "-profile:v", str(profile)]

    # Color metadata passthrough
    if probe and (probe.color_space or probe.color_transfer or probe.color_primaries):
        if probe.color_primaries:
            cmd += ["-color_primaries", probe.color_primaries]
        if probe.color_transfer:
            cmd += ["-color_trc", probe.color_transfer]
        if probe.color_space:
            cmd += ["-colorspace", probe.color_space]

    # Audio: map all audio tracks if source has audio; otherwise suppress audio
    # entirely. Using -map 0:a on a silent video causes ffmpeg to fail with
    # "No Audio Input" — a common case with screen-recordings and action cams.
    if probe and probe.audio_channels and probe.audio_channels > 0:
        cmd += ["-map", "0:a"]
        audio_codec = _audio_codec_for(probe)
        cmd += ["-c:a", audio_codec]
    else:
        cmd += ["-an"]

    # Force CFR on VFR sources (action cams, phone recordings, screen captures).
    # r_frame_rate from ffprobe is the real rate; avg_frame_rate is nominal.
    # VFR sources cause audio sync drift in proxies — CFR normalization fixes it.
    # -fps_mode cfr is applied after input decode, before output encode.
    cmd += ["-fps_mode", "cfr"]

    cmd.append(str(output))
    return cmd


def _probe_output_metadata(output: Path) -> tuple[int, int, float]:
    """Use ffprobe to get exact dimensions and duration of the generated proxy.

    Returns (width, height, duration_seconds). Falls back to zeros on probe
    failure so the caller still gets a ProxyResult.
    """
    try:
        from media_mate.probe import find_ffprobe, probe_file

        ffprobe_path = find_ffprobe()
        probe = probe_file(output, ffprobe_path=ffprobe_path)
        return (probe.width or 0, probe.height or 0, probe.duration_seconds or 0.0)
    except Exception:
        return (0, 0, 0.0)


def generate_proxy(
    request: ProxyRequest,
    ffmpeg_path: str | None = None,
) -> ProxyResult:
    """Generate a single proxy file via ffmpeg.

    Raises ProxyError if source doesn't exist, output's parent dir can't be
    created, ffmpeg isn't found, or ffmpeg returns non-zero.
    """
    source = Path(request.source_path)
    output = Path(request.output_path)

    if not source.is_file():
        raise ProxyError(source, "not a file or does not exist")

    fp = ffmpeg_path or find_ffmpeg()

    # Best-effort output directory creation.
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ProxyError(source, f"cannot create output directory {output.parent}: {e}") from e

    cmd = _ffmpeg_cmd(fp, source, output, request.codec, request.target_height, probe=request.probe)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise ProxyError(source, f"ffmpeg not found at {fp}: {e}") from e

    if result.returncode != 0:
        # ffmpeg leaves a truncated/empty output behind on failure — remove
        # it so a re-run doesn't skip the file as "already exists".
        output.unlink(missing_ok=True)
        err_lines = [line for line in result.stderr.splitlines() if line.strip()]
        last_err = err_lines[-1] if err_lines else "unknown error"
        raise ProxyError(source, f"ffmpeg exited {result.returncode}: {last_err}")

    if not output.is_file() or output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise ProxyError(source, "ffmpeg exited 0 but produced no output")

    try:
        width, height, duration = _probe_output_metadata(output)
    except Exception:
        width, height, duration = 0, 0, 0.0

    return ProxyResult(
        source_path=str(source),
        proxy_path=str(output),
        codec=request.codec,
        width=width,
        height=height,
        file_size_bytes=output.stat().st_size,
        duration_seconds=duration,
        generated_at=datetime.now(UTC),
    )


#: Extensions proxy generation will attempt — video sources only (SPEC §5.3:
#: "any ffmpeg-readable format by extension"). Everything else on a camera
#: card (subtitles, sidecar databases, checksum manifests) is skipped.
_VIDEO_EXTENSIONS = frozenset(
    {
        ".ari",
        ".avi",
        ".braw",
        ".crm",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".mxf",
        ".r3d",
    }
)


def is_video_source(path: Path) -> bool:
    """True when the file extension is a recognized video format."""
    return path.suffix.lower() in _VIDEO_EXTENSIONS


def generate_proxies(
    source: Path,
    output_dir: Path,
    store: LogStore,
    config: MediaMateConfig | None = None,
) -> ProxyBatchResult:
    """Generate proxies for all video files in source (file or directory).

    Output paths mirror the relative path under source with a `.mov`
    extension — ProRes belongs in a QuickTime container regardless of the
    source container. For example, `raw/sub1/clip.MP4` lands at
    `output_dir/sub1/clip.mov`.

    Non-video files (by extension) are excluded from the batch and listed
    in the returned skipped list. Files where the proxy already exists are
    recorded as failures (not overwritten). Each generated proxy is
    recorded in the proxies table of the audit log.

    Run status:
        - SUCCESS: every attempted video file generated successfully
        - PARTIAL: some succeeded, some failed (skip / OSError / ffmpeg error)
        - FAILED: no proxies generated
    """
    source = Path(source)
    output_dir = Path(output_dir)
    cfg = config or MediaMateConfig()

    if source.is_file():
        # For a single file, the "relative path" is just the filename.
        files: list[tuple[Path, str]] = [(source, source.name)]
    elif source.is_dir():
        files = [(p, str(p.relative_to(source))) for p in sorted(source.rglob("*")) if p.is_file()]
    else:
        raise ProxyError(source, "not a file or directory")

    skipped = [str(f) for f, _ in files if not is_video_source(f)]
    files = [(f, rel) for f, rel in files if is_video_source(f)]

    if not files:
        return ProxyBatchResult(skipped=skipped)

    ffmpeg_path = find_ffmpeg(cfg)
    from media_mate.probe import ProbeError, find_ffprobe, probe_file

    ffprobe_path = find_ffprobe(cfg)
    command = f"media-mate proxy {source} --out {output_dir}"
    run_id = store.start_run(command, config_hash=cfg.config_hash())

    results: list[ProxyResult] = []
    failures: list[tuple[Path, str]] = []
    already_existed: list[tuple[Path, Path]] = []

    for f, rel in files:
        out = (output_dir / rel).with_suffix(".mov")

        try:
            if out.exists():
                already_existed.append((f, out))
                continue

            # Probe the source to get accurate metadata for ffmpeg flags.
            probe: MediaProbe | None = None
            with suppress(ProbeError):
                probe = probe_file(f, ffprobe_path=ffprobe_path)

            request = ProxyRequest(
                source_path=str(f),
                output_path=str(out),
                codec=cfg.proxy_codec,
                target_height=cfg.proxy_height,
                probe=probe,
            )

            # Reject RAW codecs that stock ffmpeg cannot decode.
            # These require vendor SDKs (RED, Blackmagic, ARRI).
            # Container is recognized but decode will fail with a cryptic error.
            raw_codecs = {"r3d", "braw", "ari"}
            if probe and probe.video_codec and probe.video_codec.lower() in raw_codecs:
                failures.append(
                    (
                        f,
                        f"RAW codec '{probe.video_codec}' requires vendor SDK; stock ffmpeg cannot decode",
                    )
                )
                continue

            result = generate_proxy(request, ffmpeg_path=ffmpeg_path)
            results.append(result)

            file_id = store.upsert_file(
                str(f),
                size=f.stat().st_size if f.exists() else None,
                mtime=f.stat().st_mtime if f.exists() else None,
                run_id=run_id,
            )
            store.insert_proxy(
                ProxyRecord(
                    source_file_id=file_id,
                    proxy_path=str(out),
                    run_id=run_id,
                    codec=result.codec,
                    width=result.width,
                    height=result.height,
                    file_size=result.file_size_bytes,
                    generated_at=result.generated_at,
                )
            )
        except ProxyError as e:
            failures.append((e.path, e.reason))
        except OSError as e:
            failures.append((f, str(e)))

    if not failures:
        status = RunStatus.SUCCESS
        error_msg = None
    elif results:
        status = RunStatus.PARTIAL
        error_msg = _format_errors(failures)
    else:
        status = RunStatus.FAILED
        error_msg = _format_errors(failures)

    store.finish_run(run_id, status, error_msg)
    return ProxyBatchResult(
        results=results,
        failures=[ProxyFailure(source_path=str(p), reason=r) for p, r in failures],
        skipped=skipped,
        already_existed=[
            ProxySkip(source_path=str(s), proxy_path=str(o)) for s, o in already_existed
        ],
    )


def _format_errors(errors: list[tuple[Path, str]], limit: int = 5) -> str:
    """Format an error summary for the run log."""
    if not errors:
        return ""
    head = "; ".join(f"{p.name}: {r}" for p, r in errors[:limit])
    if len(errors) > limit:
        head += f"; ... ({len(errors) - limit} more)"
    return f"{len(errors)} file(s) failed: {head}"


__all__ = [
    "ProxyError",
    "find_ffmpeg",
    "generate_proxies",
    "generate_proxy",
    "is_video_source",
]
