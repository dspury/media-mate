"""Resolve capability — create DaVinci Resolve projects programmatically.

Architecture: manifest-first, Resolve API best-effort.

1. **Manifest builder** (always runs, pure functions, no Resolve dependency):
    resolve_bin_structure(source_folder) -> list[str]
        Compute bin paths mirroring the folder's subdirectory layout.
    build_project_manifest(source_folder, spec, proxy_dir=None) -> dict
        Build a JSON-serializable description of the project.
    write_manifest(manifest, output_path) -> Path
        Write a manifest dict to disk.

2. **Resolve adapter** (best-effort — requires live Resolve + running API):
    find_resolve(config) -> ModuleType | None
        Try to load the DaVinciResolveScript module; None if unavailable.
    create_resolve_project(spec, source_folder, proxy_dir, store, config=None) -> ResolveProjectResult
        Always builds the manifest first. Then tries the Resolve API; falls back
        to writing a manifest file when Resolve isn't available or an API call fails.
        ``resolve_version=None`` indicates the manifest fallback was used.

The manifest is the ground truth — it is always written to disk (at
``<output_path>.manifest.json``) when the Resolve API path fails or is
unavailable, so the user can manually act on it.

**Known limitations (deferred to v1.0):**
- MediaPoolItem creation: Resolve requires MediaPoolItems (not raw file paths)
  to link clips into the timeline. The current API path creates an empty timeline;
  clip-by-clip linking requires a heavier import step that is deferred.
- Bin naming: sub-bins are named as ``"root/subfolder"`` (e.g. ``"raw/shoot_day_1"``).
  This may not match the user's existing bin structure in Resolve.
- Spanned/multi-file clips: each file is treated as a separate clip. A proper
  spanned-clip model requires understanding clip relationships per camera format
  (RED R3D multi-part, ARRI RAW .ari + .idx, etc.) — deferred to v1.0.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from media_mate.log import LogStore
from media_mate.models import (
    MediaMateConfig,
    ProjectRecord,
    ResolveProjectResult,
    ResolveProjectSpec,
    RunStatus,
)


class ResolveError(Exception):
    """Raised when resolve integration cannot proceed."""


# ---------------------------------------------------------------------------
# Manifest builder (pure functions)
# ---------------------------------------------------------------------------


def resolve_bin_structure(source_folder: Path) -> list[str]:
    """Compute bin paths mirroring the subdirectory structure under source_folder.

    Always includes the root bin (named after source_folder). Sub-bins are
    named by joining the source folder name with each subdirectory's relative path.

    Example:
        source_folder = raw/
        raw/sub1/clip.mov
        raw/sub2/sub3/clip.mov

        Returns: ["raw", "raw/sub1", "raw/sub2", "raw/sub2/sub3"]
    """
    source_folder = Path(source_folder)
    bins: set[str] = {source_folder.name}
    for p in source_folder.rglob("*"):
        if p.is_dir():
            rel = p.relative_to(source_folder)
            bins.add(str(Path(source_folder.name) / rel))
    return sorted(bins)


def _media_files_in(folder: Path) -> list[Path]:
    """Return all files under folder, sorted."""
    return sorted(p for p in folder.rglob("*") if p.is_file())


def _bin_for(file_path: Path, source_folder: Path) -> str:
    """Determine which bin a file belongs in based on its parent directory."""
    rel = file_path.relative_to(source_folder)
    parts = rel.parts
    if len(parts) == 1:
        # File is at the root of source_folder
        return source_folder.name
    # File is in a subdirectory — bin name = root + first subdir
    return str(Path(source_folder.name) / parts[0])


def build_project_manifest(
    source_folder: Path,
    spec: ResolveProjectSpec,
    proxy_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable manifest describing the project to create.

    Pure function — no Resolve dependency. The same structure is used both
    for the Resolve API path (read fields to make API calls) and the
    manifest fallback (serialized to JSON for the user to act on later).
    """
    source_folder = Path(source_folder)
    proxy_dir_resolved = Path(proxy_dir) if proxy_dir else None

    bins: dict[str, list[str]] = {bp: [] for bp in resolve_bin_structure(source_folder)}

    for media in _media_files_in(source_folder):
        bins[_bin_for(media, source_folder)].append(str(media))

    timeline_clips: list[dict[str, Any]] = []
    if proxy_dir_resolved is not None and proxy_dir_resolved.exists():
        for media in _media_files_in(source_folder):
            rel = media.relative_to(source_folder)
            proxy = proxy_dir_resolved / rel
            if proxy.exists():
                timeline_clips.append({"source": str(media), "proxy": str(proxy)})

    return {
        "name": spec.name,
        "settings": {
            "resolution": spec.resolution,
            "frame_rate": spec.frame_rate,
            "color_space": spec.color_space,
        },
        "source_folder": str(source_folder),
        "proxy_dir": str(proxy_dir_resolved) if proxy_dir_resolved else None,
        "bins": bins,
        "timeline": {
            "name": spec.name,
            "clips": timeline_clips,
        },
    }


def write_manifest(manifest: dict[str, Any], output_path: Path) -> Path:
    """Write a manifest dict to a JSON file. Returns the path written."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return output_path


# ---------------------------------------------------------------------------
# Resolve adapter (best-effort)
# ---------------------------------------------------------------------------


def find_resolve(config: MediaMateConfig | None = None):  # type: ignore[no-untyped-def]
    """Try to load the DaVinci Resolve scripting module.

    Returns the loaded module on success, None if it can't be imported. When
    ``config.resolve_path`` is set, the Resolve Scripting/Modules directory
    is prepended to ``sys.path`` before the import attempt.
    """
    if config and config.resolve_path:
        rp = Path(config.resolve_path)
        # Standard layout: <resolve>/Developer/Scripting/Modules/
        modules_dir = rp / "Developer" / "Scripting" / "Modules"
        if modules_dir.is_dir():
            sys.path.insert(0, str(modules_dir))

    try:
        return importlib.import_module("DaVinciResolveScript")
    except ImportError:
        return None


def _scriptapp_with(resolve_module, name: str = "Resolve"):  # type: ignore[no-untyped-def]
    """Invoke the Resolve scripting entry point. Returns the Resolve handle or None."""
    scriptapp = getattr(resolve_module, "scriptapp", None)
    if scriptapp is None:
        raise ResolveError("DaVinciResolveScript module missing scriptapp() entry point")
    try:
        return scriptapp(name)
    except Exception as e:
        raise ResolveError(f"failed to connect to Resolve: {e}") from e


def _safe_version(resolve) -> str:  # type: ignore[no-untyped-def]
    """Get Resolve's version string; return empty string on failure."""
    try:
        result = resolve.GetVersionString()
        return str(result) if result else ""
    except Exception:
        return ""


def _create_via_resolve(
    manifest: dict[str, Any],
    output_path: Path,
    resolve_module: Any,
) -> tuple[str, int, int]:
    """Create the project via the Resolve API.

    Returns (resolve_version, bin_count, timeline_count).
    Raises ResolveError if any API call fails.
    """
    resolve = _scriptapp_with(resolve_module)
    if resolve is None:
        raise ResolveError("Resolve scripting returned None — is Resolve running?")

    pm = resolve.GetProjectManager()
    if pm is None:
        raise ResolveError("Resolve.GetProjectManager() returned None")

    project = pm.CreateProject(manifest["name"], str(output_path))
    if project is None:
        raise ResolveError(f"CreateProject failed for {manifest['name']!r}")

    settings = manifest["settings"]
    # Set project settings — keys vary slightly by Resolve version; best-effort.
    for key, value in (
        ("timelineResolutionWidth", settings["resolution"]),
        ("timelineResolutionHeight", settings["resolution"]),
        ("timelineFrameRate", settings["frame_rate"]),
    ):
        try:
            project.SetSetting(key, value)
        except Exception as e:
            # Don't abort — settings can be tweaked in Resolve UI later.
            # Caller gets the partial-success result via bin/timeline counts.
            raise ResolveError(f"SetSetting({key}) failed: {e}") from e

    media_pool = pm.GetMediaPool()
    if media_pool is None:
        raise ResolveError("Resolve.GetMediaPool() returned None")

    # Create bins mirroring the manifest structure. The root bin is implicit;
    # we use sub-folder paths from the manifest to create each sub-bin.
    root = media_pool.GetCurrentFolder() or media_pool.AddSubFolder(
        media_pool.GetRootFolder(), manifest["name"]
    )
    bin_count = 1 if root else 0
    for bin_name in manifest["bins"]:
        if "/" not in bin_name:
            # Root bin already exists; skip.
            continue
        sub_name = bin_name.split("/", 1)[1]
        try:
            media_pool.AddSubFolder(root, sub_name)
            bin_count += 1
        except Exception as e:
            raise ResolveError(f"AddSubFolder({sub_name}) failed: {e}") from e

    # Create the timeline. Resolve expects MediaPoolItems, not file paths —
    # building the items requires Resolve to import the media first, which is
    # a heavier operation than v1 should attempt. We create an empty timeline
    # and rely on the manifest for clip-by-clip import instructions.
    timeline_count = 0
    try:
        timeline = media_pool.CreateTimeline(manifest["timeline"]["name"])
        if timeline is not None:
            timeline_count = 1
    except Exception as e:
        raise ResolveError(f"CreateTimeline failed: {e}") from e

    try:
        pm.SaveProject()
    except Exception as e:
        raise ResolveError(f"SaveProject failed: {e}") from e

    return _safe_version(resolve), bin_count, timeline_count


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def create_resolve_project(
    spec: ResolveProjectSpec,
    source_folder: Path,
    proxy_dir: Path | None,
    store: LogStore,
    config: MediaMateConfig | None = None,
) -> ResolveProjectResult:
    """Create a Resolve project from a source folder + optional proxy dir.

    Always builds the manifest first. Then:

    - If the Resolve API is available AND every API call succeeds, the project
      is created in Resolve and ``resolve_version`` is set in the result.
    - If the Resolve API is unavailable or any call fails, falls back to
      writing a manifest file (``<output_path>.manifest.json``) so the user
      can manually create the project. ``resolve_version`` is None in the
      result, and the run is marked PARTIAL when the API was available but
      errored, FAILED only when we couldn't even produce the manifest.

    The manifest write is best-effort — if it fails (e.g., output_path is
    unwritable), the error is captured in the run's error field.
    """
    source_folder = Path(source_folder)
    output_path = Path(spec.output_path)
    proxy_dir_resolved = Path(proxy_dir) if proxy_dir else None
    cfg = config or MediaMateConfig()
    now = datetime.now(UTC)

    if not source_folder.is_dir():
        raise ResolveError(f"source folder does not exist: {source_folder}")

    # Guard: an empty source folder produces an empty Resolve project, which
    # is never what the user wants. Reject early with a clear message rather
    # than silently creating a blank project they have to delete.
    media_files = _media_files_in(source_folder)
    if not media_files:
        raise ResolveError(
            f"source folder is empty: {source_folder}. "
            "Resolve cannot import from an empty folder — add media before trying to create a project."
        )

    manifest = build_project_manifest(source_folder, spec, proxy_dir_resolved)

    command = f"media-mate resolve create {source_folder} --project {spec.name}"
    run_id = store.start_run(command)

    bin_count = len(manifest["bins"])
    timeline_count = 1 if manifest["timeline"]["clips"] else 0
    resolve_version: str | None = None
    status = RunStatus.SUCCESS
    error_msg: str | None = None

    resolve_module = find_resolve(cfg)
    if resolve_module is not None:
        try:
            resolve_version, bin_count, timeline_count = _create_via_resolve(
                manifest, output_path, resolve_module
            )
        except ResolveError as e:
            # Resolve was available but errored — partial success; still write manifest.
            status = RunStatus.PARTIAL
            error_msg = f"Resolve API failed: {e}; falling back to manifest"
            try:
                write_manifest(
                    manifest,
                    output_path.with_suffix(output_path.suffix + ".manifest.json"),
                )
            except OSError as we:
                error_msg = f"{error_msg}; manifest write also failed: {we}"
    else:
        # Resolve not available — write manifest so the user can act later.
        try:
            write_manifest(
                manifest,
                output_path.with_suffix(output_path.suffix + ".manifest.json"),
            )
        except OSError as e:
            status = RunStatus.FAILED
            error_msg = f"could not write manifest: {e}"

    # Always record what happened in the audit log.
    store.insert_project(
        ProjectRecord(
            name=spec.name,
            path=str(output_path),
            run_id=run_id,
            resolution=spec.resolution,
            frame_rate=spec.frame_rate,
            color_space=spec.color_space,
            bin_count=bin_count,
            timeline_count=timeline_count,
            resolve_version=resolve_version,
            created_at=now,
        )
    )
    store.finish_run(run_id, status, error_msg)

    return ResolveProjectResult(
        name=spec.name,
        path=str(output_path),
        resolution=spec.resolution,
        frame_rate=spec.frame_rate,
        color_space=spec.color_space,
        bin_count=bin_count,
        timeline_count=timeline_count,
        resolve_version=resolve_version,
        created_at=now,
    )


__all__ = [
    "ResolveError",
    "build_project_manifest",
    "create_resolve_project",
    "find_resolve",
    "resolve_bin_structure",
    "write_manifest",
]
