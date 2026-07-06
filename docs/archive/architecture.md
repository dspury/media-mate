# Architecture

This document describes how media-mate is built: the capabilities, the data flow between them, the audit-log schema, and how the code is organized.

## High-level

media-mate is a single Python package (`media_mate`) with six capability modules and one data layer:

```
                    ┌────────────────────────────────────┐
                    │            media-mate CLI          │
                    │           (cli.py, Click)          │
                    └──────┬───────┬───────┬───────┬──────┘
                           │       │       │       │
              ┌────────────┘       │       │       └────────────┐
              │                    │       │                    │
              ▼                    ▼       ▼                    ▼
        ┌──────────┐        ┌──────────┐ ┌──────────┐       ┌──────────┐
        │  probe   │        │ organize │ │  proxy   │       │  verify  │
        │  (probe  │        │(organize │ │ (proxy   │       │ (verify  │
        │   .py)   │        │   .py)   │ │   .py)   │       │   .py)   │
        └────┬─────┘        └────┬─────┘ └────┬─────┘       └────┬─────┘
             │                   │            │                  │
             │  ffprobe          │            │  ffmpeg           │  xxhash /
             │                   │            │                   │  sha256
             │                   │            │                   │
             ▼                   ▼            ▼                   ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                       LogStore (log.py)                       │
        │                  SQLite audit database                        │
        │         ~/.media-mate/media-mate.db (default)                │
        └──────────────────────────────────────────────────────────────┘
                                       ▲
                                       │ also writes to
                                       │
                                ┌──────────────┐
                                │   resolve    │
                                │  (resolve.py)│
                                └──────┬───────┘
                                       │
                                       │  DaVinciResolveScript
                                       ▼
                              DaVinci Resolve (when running)
                                       │
                                       │ or, when unavailable:
                                       ▼
                              <output>.manifest.json
```

Each capability is an independent Python module that can be run standalone or composed via the CLI. Every capability writes its results to the audit log so operations are traceable.

## Capabilities

| Module | Public API | External tool |
|---|---|---|
| `probe` | `find_ffprobe()`, `probe_file()`, `probe_path()` | ffprobe |
| `organize` | `codec_family()`, `resolution_bucket()`, `organize_path()` | (filesystem) |
| `proxy` | `find_ffmpeg()`, `generate_proxy()`, `generate_proxies()` | ffmpeg |
| `verify` | `compute_checksum()`, `verify_folder()` | (filesystem + xxhash/sha256) |
| `resolve` | `find_resolve()`, `build_project_manifest()`, `create_resolve_project()` | DaVinci Resolve scripting API (or filesystem) |
| `config` | `load_config()` | (Python tomllib) |

The `cli.py` module is the wiring layer that exposes these capabilities as Click commands.

## Data layer

`media_mate.models` defines pydantic v2 models for every concept in the system:

- **Capability I/O:** `MediaProbe`, `OrganizeConfig`/`OrganizeResult`, `ProxyRequest`/`ProxyResult`, `ResolveProjectSpec`/`ResolveProjectResult`, `VerificationReport`, `MediaMateConfig`
- **Persistence records:** `RunRecord`, `FileRecord`, `ProbeRecord`, `ProxyRecord`, `ProjectRecord`, `VerificationRecord`, `VerificationSnapshotRecord`, `OrganizeOpRecord`
- **Enums:** `RunStatus`, `ChecksumAlgo`

These models are the public schema of the package. Anyone using media-mate as a library (vs. via the CLI) interacts with these types.

`media_mate.log` provides `LogStore` — a SQLite-backed audit log with:

- Parameterized queries (no SQL injection)
- Context-managed transactions with rollback on error
- Idempotent schema initialization (`SCHEMA_VERSION = 3`)
- Per-table insert + query methods
- A bulk `replace_verification_snapshot` operation for atomic snapshot replacement

The DB is created on first use at `~/.media-mate/media-mate.db`. The parent directory is created automatically.

## SQLite schema (v3)

```
┌─────────────────────────────────────────────────────────────────────┐
│ runs                                                                │
├─────────────────────────────────────────────────────────────────────┤
│ id (PK) | started_at | finished_at | command | config_hash |        │
│ status | error                                                      │
└─────────────────────────────────────────────────────────────────────┘
       │ (1:N)
       ▼
┌──────────────────────┐    ┌──────────────────────┐    ┌─────────────┐
│ probes               │    │ proxies              │    │ projects    │
├──────────────────────┤    ├──────────────────────┤    ├─────────────┤
│ id (PK)              │    │ id (PK)              │    │ id (PK)     │
│ file_id (FK files)   │    │ source_file_id (FK)  │    │ name        │
│ run_id (FK runs)     │    │ proxy_path           │    │ path        │
│ codec | container    │    │ run_id (FK runs)     │    │ run_id (FK) │
│ width | height       │    │ codec                │    │ resolution  │
│ frame_rate           │    │ width | height       │    │ frame_rate  │
│ color_space          │    │ file_size            │    │ color_space │
│ bit_depth            │    │ generated_at         │    │ bin_count   │
│ duration             │    └──────────────────────┘    │ timeline_…  │
│ audio_channels       │                              │ resolve_ver │
│ audio_sample_rate    │                              │ created_at  │
│ probed_at            │                              └─────────────┘
└──────────────────────┘
       ▲                                              ┌─────────────────┐
       │                                              │ verifications    │
┌──────────────────────┐                              ├─────────────────┤
│ files                │                              │ id (PK)         │
├──────────────────────┤                              │ folder          │
│ id (PK)              │                              │ run_id (FK)     │
│ path (UNIQUE)        │                              │ files_checked   │
│ size | mtime         │                              │ files_missing   │
│ first_seen_run (FK)  │                              │ files_modified  │
│ last_seen_run (FK)   │                              │ files_added     │
└──────────────────────┘                              │ checksum_algo   │
                                                     │ verified_at     │
┌──────────────────────┐                              └─────────────────┘
│ organize_ops         │
├──────────────────────┤                              ┌──────────────────────┐
│ id (PK)              │                              │ verification_snapshots│
│ run_id (FK runs)     │                              ├──────────────────────┤
│ source_path          │                              │ id (PK)              │
│ destination_path     │                              │ folder | path        │
│ codec_family         │                              │ checksum              │
│ resolution_bucket    │                              │ size | mtime         │
│ file_size            │                              │ algo                  │
│ moved_at             │                              │ recorded_at          │
└──────────────────────┘                              │ UNIQUE(folder, path)  │
                                                     └──────────────────────┘
```

Every capability writes to one or more of these tables. The audit log is the source of truth for "what happened."

## Data flow examples

### Probe → Organize

```
1. User: media-mate organize ~/raw/ --root ~/organized/
2. organize_path() queries store.get_latest_probes_by_paths() to get
   codec/resolution for every file in ~/raw/.
3. Files are classified into codec_family (prores/h264/...) and
   resolution_bucket (480p/720p/1080p/4K/...).
4. For each file, a destination path is rendered from the template.
5. Files are moved (or skipped on conflict) and an organize_ops row
   is written for each move.
6. The run is closed with a SUCCESS/PARTIAL/FAILED status.
```

### Verify → cron

```
1. User: media-mate verify ~/backup/
2. verify_folder() looks up the previous snapshot for ~/backup/.
3. Current checksums are computed (xxhash by default; 64KB chunks).
4. Diff against the snapshot: missing / modified / added.
5. The new snapshot replaces the old.
6. Exit code is 0/1/2/3 per the priority in SPEC.md §5.5.
7. Cron can switch on the exit code to alert.
```

### Resolve create (graceful fallback)

```
1. User: media-mate resolve create ~/raw/ --project "Episode-12"
2. The manifest is always built first (pure function).
3. find_resolve() tries to import DaVinciResolveScript.
4a. If available: the Resolve API is called (CreateProject, SetSetting,
    AddSubFolder, CreateTimeline, SaveProject).
4b. If unavailable: a JSON manifest is written next to the intended
    .drp path; resolve_version in the result is None.
5. Either way, the project_record is logged and a ResolveProjectResult
   is returned.
```

## Module layout

```
src/media_mate/
├── __init__.py        # version + __all__
├── cli.py             # Click commands
├── config.py          # TOML config loader
├── models.py          # Pydantic schemas
├── log.py             # LogStore (SQLite)
├── probe.py           # ffprobe wrapper
├── organize.py        # File organizer
├── proxy.py           # ffmpeg wrapper
├── verify.py          # Checksum verification
└── resolve.py         # DaVinci Resolve integration
```

Every module except `cli.py` and `config.py` can be imported as a library without pulling in the CLI machinery.

## Adding a new capability

To add a new capability (say, scene detection):

1. Create `src/media_mate/scenes.py` with the capability logic.
2. Add pydantic I/O models to `models.py` (`SceneDetectionRequest`, `SceneDetectionResult`).
3. Add a SQLite table to `log.py` (bump `SCHEMA_VERSION`, add `INSERT INTO ...`, add a `LogStore` method).
4. Add a Click command to `cli.py`.
5. Add tests under `tests/test_scenes.py`.
6. Update `README.md` and `examples/sample-run.md`.

Every capability follows the same shape:

- One public function for single-file operation.
- One public function for batch operation (takes a `LogStore`, writes audit rows).
- An error exception class.
- A "graceful fallback" pattern when external tools aren't available.

## Testing approach

- **Unit tests** mock external tools (`subprocess.run`, `DaVinciResolveScript`) so the package can be tested without FFmpeg or Resolve installed.
- **Integration tests** for any code path that doesn't require external tools (model validation, schema migrations, the manifest builder).
- The CI matrix runs on Python 3.11, 3.12, and 3.13 with FFmpeg installed via apt. Resolve is not installed in CI; the manifest-fallback path is what gets exercised.

All tests live under `tests/` and use pytest. The `store_dir` fixture pattern (using `tmp_path_factory.mktemp`) is used throughout to keep the audit log outside any directory being processed — a subtle gotcha we hit early in development.

## Configuration precedence

media-mate has three layers of configuration, in order of precedence:

1. CLI flags: `--db`, `--config`
2. Environment variables: `MEDIA_MATE_DB`, `MEDIA_MATE_CONFIG`
3. Config file (`media-mate.toml`)
4. Built-in defaults

Within the config file, any field can be omitted — pydantic fills in the default.

## Safety guarantees

- **No hardcoded paths** anywhere in the code. Every filesystem path is configurable.
- **No hostnames / IPs / cloud account references** — the package is open-source safe.
- **Public tools only** — FFmpeg, xxhash, SQLite, Python's tomllib. No proprietary dependencies.
- **No telemetry, no remote calls, no network access** of any kind. Truly local.
- **Test fixtures use synthetic data** — no real media files checked in.

These constraints are baked into the package metadata (license, classifiers) and enforced in CI (lint rules, dependency checks).