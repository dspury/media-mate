# media-mate — Spec v0.2.2

> **Name:** `media-mate`
> **Repo location:** `dspury/media-mate`
> **Version:** 0.2.2
> **Status:** Released — stable

---

## 1. One-liner

**A zero-cost CLI for post-production media ops: probe, organize, generate proxies, build DaVinci Resolve projects, and verify backups — all logged to a local SQLite audit trail.**

The killer demo: drop a folder of raw media in, run `media-mate run`, and walk away with organized folders, ready-to-edit proxies, a Resolve project file pre-wired with bins and a timeline, and a queryable SQLite audit log proving exactly what happened.

---

## 2. Why this exists

**Target user:** Solo creative operators, small post-production teams, anyone running DaVinci Resolve or other NLEs who needs reliable media infrastructure underneath their edit.

**Problem it solves:** Most post-production tooling is either (a) expensive SaaS, (b) manual point-and-click workflows that don't scale, or (c) one-off scripts glued together. There is no widely-adopted open-source tool that handles the boring-but-critical media-ops layer (probe → organize → proxy → Resolve project → verify) in one composable, logged, reproducible package.

**What it demonstrates:** Production-engineering instincts applied to creative media. Codec literacy, FFmpeg fluency, Resolve's Python scripting API, schema thinking, audit discipline.

---

## 3. Goals

1. **Zero cost to run.** Every dependency is open-source or free. No API keys, no cloud accounts, no paid SaaS.
2. **Single-operator CLI + TUI.** Sharp CLI for scripting and automation; interactive Textual TUI (`media-mate tui`) for ad-hoc use. No web UI.
3. **Composable pipeline.** Each capability (probe / organize / proxy / resolve / verify) is independent and can be run standalone or chained.
4. **Auditable by default.** Every operation writes to a local SQLite log. The log is the system of record for "what happened to my media."
5. **Safe to open-source.** No hardcoded paths, hostnames, NAS shares, IPs, or proprietary references anywhere.
6. **Industry-tool native.** DaVinci Resolve integration where it adds value, FFmpeg fallback where it doesn't.
7. **Reproducible.** Re-running a `media-mate run` on the same input produces the same output and the same log row, modulo timestamps.

---

## 4. Non-goals (explicit, with reasons)

| Excluded | Why |
|---|---|
| Transcription (Whisper) | Out of media-management scope; product is infrastructure, not creative AI |
| LLM-based content description | Same reason |
| Auto-selects / scoring | Creative-AI territory, not infrastructure |
| Web UI | CLI + TUI in v1; browser UI is a v2+ concern |
| Cloud APIs (any vendor) | Zero-cost mandate; local-first mandate |
| Team collaboration features | Single-operator scope |
| Auto-tagging / ML classification | Out of scope for v1; v2 candidate |
| Scene detection | v2 candidate (PySceneDetect is a natural fit, but defer) |
| Audio loudness / color analysis | v2 candidate |
| Editing features (cuts, transitions) | Not the tool's job; Resolve does this |

---

## 5. v1 Scope — the six capabilities

### 5.1 Probe

Extract structured metadata from any media file (video / audio / image) using `ffprobe`. Output is a `pydantic` model capturing:

- `codec`, `container`, `width`, `height`, `frame_rate`, `avg_frame_rate`, `r_frame_rate`
- `color_space`, `color_transfer`, `color_primaries`
- `bit_depth` (from `bits_per_raw_sample`; falls back to parsing `pix_fmt` for formats that don't report it — e.g., ProRes)
- `is_vfr` — `True` when `r_frame_rate` differs from `avg_frame_rate` by more than 1%; indicates variable-frame-rate source
- `sample_aspect_ratio` (SAR) — e.g., `"2:1"` for anamorphic sources
- `timecode` — extracted from `format.tags.timecode` or video stream `disposition.timecode`
- `audio_codec`, `audio_channels`, `audio_sample_rate`, `audio_bit_depth`
- `duration`, `file_size`, modification time

JSON-serializable, queryable in SQLite.

### 5.2 Organize

Auto-organize a folder of media into a structured layout based on configurable rules. Default rule: `<root>/<source_relpath>/<filename><ext>` — the source's subfolder structure is preserved under the destination root (mirrors how DITs think about cards/scenes/takes). Rules live in a config file (`media-mate.toml`) and can be overridden per-project (e.g. `{root}/{codec_family}/{resolution_bucket}/{filename}{ext}`). Sources are copied by default so raw camera media stays untouched; `--move` (or `mode = "move"` in config) relocates instead.

**Note:** `--dry-run` is supported — preview the organization plan before touching any files.

Each operation is logged; the manifest is reversible.

### 5.3 Proxy generation

Generate edit-friendly proxies (default: ProRes 422 Proxy at 1080p, aspect-preserving) from raw camera formats. Output is always a `.mov` QuickTime file regardless of source container; non-video files are excluded by extension.

**Proxy generation is probe-informed.** Before generating, the source is probed and the following metadata is used to build the correct ffmpeg command:

- **Timecode** — passed via `-timecode` flag when source carries timecode
- **Color metadata** — `-color_primaries`, `-color_trc`, `-colorspace` passed through from source
- **SAR / anamorphic** — `setsar` applied after scale to restore correct display aspect ratio
- **Audio codec** — PCM bit depth matched to source audio (`pcm_s16le` for 8–15-bit audio, `pcm_s32le` for 16+ bit)
- **All audio tracks** — `-map 0:a` captures every audio track, not just the first

On same-device organize operations, hardlinks are used instead of full copies to avoid wasted I/O.

Supports MOV, MXF, MP4, and any ffmpeg-readable format. **RAW codecs (R3D/BRAW/ARI) are recognized by container but require vendor SDKs for decode — stock ffmpeg cannot decode them.**

### 5.4 DaVinci Resolve project creation

Programmatically create a Resolve project (`.drp`) from a manifest + a config. Sets project resolution / frame rate / color space; creates a bin structure mirroring the source folder; imports media into the appropriate bins; creates a timeline pre-populated with proxy references. Graceful degradation: if Resolve isn't running/installed, emits a "ready to import" manifest instead and logs a warning.

### 5.5 Backup verification

Compute fast checksums (xxhash by default; sha256 optional) of all files in a folder; store in SQLite. `media-mate verify` compares current state vs recorded state and reports missing / modified / added files with structured exit codes (0 = clean, 1 = missing, 2 = modified, 3 = added). Designed for shell scripting and cron.

**Baseline mutability:** Verification does NOT automatically update the stored baseline on mismatch. A mismatch is always reported as an error until explicitly acknowledged. This prevents silent bit-rot: a corrupted file that was missed once does not suppress future detections by overwriting the baseline.

### 5.6 SQLite audit log (the system of record)

Every operation writes to a local SQLite database (`~/.media-mate/media-mate.db` by default). Schema covers: runs, files, probes, proxies, projects, verifications, errors. Queryable via `media-mate log` subcommand (e.g., `media-mate log --since 1d --missing`).

---

## 6. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                   media-mate CLI + TUI                       │
│            (Click for CLI; Textual for TUI)                  │
│                                                               │
│    CLI:   media-mate probe / organize / proxy / verify ...  │
│    TUI:   media-mate tui   (interactive full-screen app)     │
└──────┬──────────┬──────────┬──────────┬───────────┬──────────┘
       │          │          │          │           │
       ▼          ▼          ▼          ▼           ▼
   ┌───────┐  ┌───────┐  ┌───────┐  ┌───────┐  ┌─────────┐
   │ Probe │  │Organiz│  │Proxy  │  │Resolve│  │ Verify  │
   │       │  │  e    │  │ Gen   │  │ Create│  │         │
   └───┬───┘  └───┬───┘  └───┬───┘  └───┬───┘  └────┬────┘
       │          │          │          │            │
       │        ffprobe    ffmpeg   Resolve.py    xxhash
       │          │          │          │  (or      │
       │          │          │          │   ffmpeg  │
       │          │          │          │ fallback) │
       │          │          │          │           │
       └──────────┴────┬─────┴──────────┴───────────┘
                       │
                       ▼
            ┌─────────────────┐
            │  SQLite Audit   │
            │     Log         │
            │ (system of     │
            │  record)        │
            └─────────────────┘
```

Each box is a Python module with its own tests. The CLI and TUI both compose them.

---

## 7. Data model (SQLite schema sketch)

```sql
-- One row per media-mate run
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    command TEXT NOT NULL,           -- e.g., "media-mate run /path/to/folder"
    config_hash TEXT,                -- hash of the media-mate.toml used
    status TEXT NOT NULL,            -- running | success | failed | partial
    error TEXT
);

-- One row per file the system has ever seen
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,       -- absolute path
    size INTEGER,
    mtime REAL,
    first_seen_run INTEGER REFERENCES runs(id),
    last_seen_run INTEGER REFERENCES runs(id)
);

-- Probe results
CREATE TABLE probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES files(id),
    run_id INTEGER REFERENCES runs(id),
    codec TEXT,
    container TEXT,
    width INTEGER,
    height INTEGER,
    frame_rate REAL,
    avg_frame_rate REAL,
    r_frame_rate REAL,
    color_space TEXT,
    color_transfer TEXT,
    color_primaries TEXT,
    bit_depth INTEGER,
    sample_aspect_ratio TEXT,        -- e.g. "2:1"
    timecode TEXT,                   -- e.g. "01:23:45:12"
    is_vfr INTEGER,                  -- 1 = true, 0 = false
    audio_codec TEXT,
    audio_channels INTEGER,
    audio_sample_rate INTEGER,
    audio_bit_depth INTEGER,
    duration REAL,
    probed_at TEXT NOT NULL
);

-- Proxy generation records
CREATE TABLE proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES files(id),
    proxy_path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    codec TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    generated_at TEXT NOT NULL
);

-- Resolve project creation records
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    resolution TEXT,
    frame_rate REAL,
    color_space TEXT,
    bin_count INTEGER,
    timeline_count INTEGER,
    resolve_version TEXT,            -- e.g., "20.0"; null if FFmpeg fallback used
    created_at TEXT NOT NULL
);

-- Backup verification records
CREATE TABLE verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    files_checked INTEGER,
    files_missing INTEGER,
    files_modified INTEGER,
    files_added INTEGER,
    checksum_algo TEXT,              -- xxhash | sha256
    verified_at TEXT NOT NULL
);

-- Organize operations — one row per file moved or copied
CREATE TABLE organize_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    operation TEXT NOT NULL,          -- copy | move | link | skip
    codec TEXT,                       -- detected source codec
    resolution TEXT,                  -- detected source resolution
    organized_at TEXT NOT NULL
);

-- Verification baseline snapshots — immutable; never mutated on mismatch
CREATE TABLE verification_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    path TEXT NOT NULL,              -- relative path within folder
    checksum TEXT NOT NULL,
    size INTEGER NOT NULL,
    snapshot_at TEXT NOT NULL,
    UNIQUE(folder, path)
);
CREATE INDEX idx_verif_snap_folder ON verification_snapshots(folder);
```

---

## 8. CLI surface

```bash
# Probe a single file
media-mate probe clip.mov

# Probe a folder, write to log
media-mate probe ./raw/

# Organize a folder using rules in media-mate.toml
media-mate organize ./raw/ --root ./organized/

# Preview organize without touching files
media-mate organize ./raw/ --root ./organized/ --dry-run

# Generate proxies for everything in a folder
media-mate proxy ./organized/ --out ./proxies/

# Create a Resolve project from a folder
media-mate resolve create ./organized/ --project "Episode-12" --resolution 1080 --fps 24

# Verify a backup
media-mate verify ./raw/

# Query the audit log
media-mate log --since 1d
media-mate log --missing
media-mate log --proxies --format json

# Full pipeline
media-mate run ./raw/ --organize --proxy --resolve-project --verify
```

---

## 9. Tech stack & dependencies

| Component | Choice | License | Cost |
|---|---|---|---|
| Language | Python 3.11+ | PSF | Free |
| CLI framework | Click + Rich (TTY output) | BSD / MIT | Free |
| TUI framework | Textual (full-screen TUI, `media-mate tui`) | MIT | Free |
| Media probe | ffprobe (via ffmpeg) | LGPL/GPL | Free |
| Transcode / proxy | ffmpeg | LGPL/GPL | Free |
| Checksum | xxhash (python-xxhash) | BSD | Free |
| DB | SQLite (stdlib) | Public domain | Free |
| Models | pydantic v2 | MIT | Free |
| Terminal output | rich | MIT | Free |
| Resolve API | DaVinci Resolve scripting | Free with Resolve | Free (Resolve Studio free version supports scripting) |
| Tests | pytest | MIT | Free |
| Lint / format | ruff | MIT | Free |

**Total cost to run: $0.**

---

## 10. Repo layout

```
media-mate/
├── README.md
├── LICENSE                 (MIT)
├── pyproject.toml
├── media-mate.toml.example
├── SPEC.md                 (this document)
├── src/media_mate/
│   ├── __init__.py
│   ├── cli.py              (Click entrypoint)
│   ├── config.py           (media-mate.toml loader)
│   ├── probe.py            (ffprobe wrapper)
│   ├── organize.py         (file organizer)
│   ├── proxy.py            (proxy generation)
│   ├── resolve.py          (DaVinci project creation + ffmpeg fallback)
│   ├── verify.py           (checksum verification)
│   ├── log.py              (SQLite audit log)
│   ├── models.py           (pydantic schemas)
│   ├── errors.py           (custom exceptions with exit codes)
│   └── tui.py              (Textual TUI)
├── tests/
│   ├── test_probe.py
│   ├── test_organize.py
│   ├── test_proxy.py
│   ├── test_resolve.py
│   ├── test_verify.py
│   └── test_log.py
├── examples/
│   └── sample-run.md
└── docs/
    └── architecture.md
```

---

## 11. Safety constraints (hard rules)

1. **No hardcoded paths.** Everything configurable via CLI args, env vars, or `media-mate.toml`.
2. **No hostnames, IPs, NAS shares, or cloud account refs.** Anywhere in the code, docs, or tests.
3. **No proprietary branding or team references.** Anywhere.
4. **Public tools only.** FFmpeg, ffprobe, pydantic, Click, DaVinci Resolve (free), Python ecosystem.
5. **No paid SaaS fallbacks.** Graceful degradation if a piece isn't installed; never "send to API."
6. **No telemetry, no analytics, no remote calls.** Truly local.
7. **Test fixtures use synthetic data only.** No real media, no real names.

---

## 12. First-class terminal interface

`media-mate` with no arguments launches the Textual workstation. Automation and
the existing command surface remain available as subcommands; `media-mate
--no-tui` explicitly stays in CLI mode and prints command help. `media-mate tui`
remains as a compatibility alias. Global `--db` and `--config` options are passed
through to the TUI.

### Screen inventory

| Screen | Purpose |
|---|---|
| Home | Studio-style launch surface, ffmpeg health, database run totals |
| Pipelines | Browse mounted directories, queue multiple folders, select all five capability steps, and monitor sequential execution |
| Audit log | Browse up to 500 runs with colored status and incremental command/status search |
| Settings | Edit proxy codec/height, checksum algorithm, ffmpeg path, and Resolve path; save the existing TOML schema |

The pipeline executes `probe → organize → proxy → resolve → verify`. Each queued
folder has an independent state (`queued`, `running`, `done`, `failed`, or
`cancelled`). Capability modules remain the source of truth and are called
unchanged. Cancellation therefore occurs safely between capability calls; an
in-flight ffmpeg or checksum batch is allowed to finish rather than being killed
mid-write. The activity panel reports elapsed time, queue/step progress, and
color-coded per-capability result totals. Frame/fps/bitrate telemetry is deferred
until the proxy capability exposes a progress callback.

### Keyboard contract

| Key | Action |
|---|---|
| `R`, `L`, `S` | Open Pipelines, Audit Log, or Settings from Home |
| Arrow keys / `Tab` / `Shift+Tab` | Move through trees, tables, fields, toggles, and buttons |
| `Enter` | Expand/select a directory or activate the focused control |
| `A` | Add the selected browser folder to the pipeline queue |
| `Delete` | Remove the selected queued folder |
| `Ctrl+R` | Run the queue sequentially |
| `Ctrl+C` | Request safe cancellation after the active capability returns |
| `/` | Focus audit-log search |
| `Ctrl+S` | Save settings |
| `Escape` | Return to the previous screen |
| `Q` | Quit |

The visual system uses a near-black edit-bay background, Resolve-inspired orange
for primary actions, purple for stage transitions, cyan for telemetry, and
green/yellow/red outcome semantics. Terminal applications cannot select
proportional fonts, so hierarchy is expressed with weight, spacing, borders, and
color while metadata remains naturally monospace.

---

## 13. Versioning rule

**media-mate ships as a beta indefinitely.** The version scheme is `MAJOR.MINOR.PATCH`:

- **MAJOR** stays at **0** indefinitely. **Never bump to 1.0.0 without D's explicit approval.**
- **PATCH** bumps (0.1.0 → 0.1.1) are fine for bug fixes — autonomous.
- **MINOR** bumps (0.1.0 → 0.2.0) require D's approval — they're feature-signal events.
- **First tagged release:** 0.1.0.

---

## 13. PyPI publish

media-mate ships to PyPI under the name `media-mate`. Install becomes:

```bash
pip install media-mate
media-mate --help
```

- Python package import name: `media_mate`
- CLI command: `media-mate`
- PyPI package name: `media-mate`

Build via `python -m build`, publish via `twine upload` (or `pyproject.toml`-driven trusted publishing on GH Actions).

---

## 14. GitHub Actions CI

Workflow at `.github/workflows/ci.yml`. Matrix:

- Python 3.10, 3.11, 3.12
- Each matrix entry: install deps → `pytest` → `ruff check` → `ruff format --check`

Plus a smoke test that runs `media-mate --help` and a small end-to-end probe of a synthetic fixture.

---

## 15. Open questions — resolved during build

1. **Click vs Typer.** → **Click.** Chosen for its maturity, stable API, and rich output ecosystem (Rich). Typer rejected.
2. **Checksum algo default.** → **xxhash.** Implemented as default; sha256 available as opt-in. Speed difference is real and meaningful for large folders.
3. **Default organize rule.** → **codec_family + resolution_bucket.** Date rejected — it creates messy paths for multi-day shoots. The two-tier structure is clean and professional.
4. **Sample demo media.** → **FFmpeg testsrc synthetic clips.** Five 2-second clips (h264 + ProRes, mixed resolutions) generated with `ffmpeg -f lavfi -i testsrc`. No real media needed.

---

## 16. Future work (v2+ candidates)

- Scene detection (PySceneDetect)
- Audio loudness analysis (LUFS / true peak)
- Color analysis (histogram, dominant color extraction)
- Resolve round-trip render (export EDL → render → verify)
- Watch-folder mode (run on file appearance)
- Web UI (FastAPI + simple HTML)
- Cloud-storage adapters (S3, GCS) — careful with the "zero cost" mandate
- Full spanned/multi-file clip model (logical clip abstraction across organize/proxy/resolve)

---

## 17. Build order — completed

All items shipped in v0.1.0:

1. ✅ Scaffold `media-mate/` (pyproject.toml, src layout, tests/, ruff config)
2. ✅ CI workflow with paths-filter
3. ✅ `models.py` + `log.py` (the data layer everything else depends on)
4. ✅ `probe.py` + tests (simplest capability, validates the pipeline)
5. ✅ `organize.py` + tests (depends on probe)
6. ✅ `proxy.py` + tests (depends on probe)
7. ✅ `verify.py` + tests (independent)
8. ✅ `resolve.py` + tests (most complex, integrate last)
9. ✅ `cli.py` (wires it all together)
10. ✅ README + examples + docs
11. ✅ GitHub release only; PyPI publish deferred

---

## 18. Changes in v0.2.x

### v0.2.2 — Bug fixes and probe enrichment

**Status:** Released.

#### Bug fixes

- **Proxy command was probe-ignorant.** `generate_proxy()` never passed `request.probe` to `_ffmpeg_cmd()`. Every proxy was generated with safe defaults regardless of source metadata. Timecode, color passthrough, SAR correction, and source-matched PCM bit depth were all unreachable from the public API. Fixed: probe is now passed through.
- **Batch proxy probing always silently failed.** `ffprobe_path = find_ffmpeg(cfg)` was used instead of `find_ffprobe(cfg)`. ffmpeg rejects ffprobe-only arguments and exits with an error, which was silently swallowed. All batch proxy generation was running without probe data. Fixed: correct `find_ffprobe` now used.
- **Skip-existing proxies logged as failures.** When a proxy already existed and `--skip-existing` was set, the result was recorded as a `ProxyFailure`. Now recorded as `already_existed` — a distinct outcome, not a failure.
- **Sidecar files created probe noise.** `probe_path()` recorded every file matching known extensions, including `.pek`, `.pbf`, `.CTox`, and other non-media sidecar formats that ffprobe cannot parse. Now only files that ffprobe can actually parse are recorded.
- **`organize --dry-run` not exposed on CLI.** The underlying `organize_path()` supported `dry_run`, but the CLI never exposed it. Fixed: `--dry-run` is now a CLI flag on the organize command.

#### New capabilities

- **Bit depth from `pix_fmt`:** `MediaProbe.bit_depth` now falls back to parsing the `pix_fmt` field when `bits_per_raw_sample` is absent (common with ProRes and other intermediate codecs). Maps `yuv420p10le` → 10-bit, `yuv422p8` → 8-bit, etc.
- **VFR detection:** `MediaProbe` now captures `r_frame_rate` (real frame rate) alongside `avg_frame_rate` (nominal). `is_vfr` is `True` when they diverge by more than 1%. VFR sources (phone recordings, screen captures, action cams) are now flagged in probe output.
- **Timecode extraction:** `MediaProbe.timecode` is extracted from `format.tags.timecode` or video stream `disposition.timecode`. Proxies now carry timecode when the source has it.
- **SAR / anamorphic support:** `MediaProbe.sample_aspect_ratio` is captured from the video stream. Proxy generation now applies `setsar` to restore correct display aspect ratio after scaling, preventing anamorphic sources from producing wrong-shaped proxies.
- **Audio bit depth from probe:** Source audio bit depth is extracted from ffprobe stream data and used to select `pcm_s16le` (8–15-bit audio) or `pcm_s32le` (16+ bit audio) in the proxy command.
- **`--dry-run` on organize:** CLI preview mode. Files are planned but not moved or copied.

#### Data model changes

- `MediaProbe`: new fields — `is_vfr`, `avg_frame_rate`, `r_frame_rate`, `sample_aspect_ratio`, `timecode`, `audio_codec`, `audio_channels`, `audio_sample_rate`, `audio_bit_depth`
- `ProxyBatchResult`: new field `already_existed: list[ProxySkip]` — proxies skipped because they already existed, distinct from failures
- New model: `ProxySkip` — records a source path and proxy path for each skipped file

#### Resolved issues

Closed in v0.2.2: #7 (SAR), #17 (--dry-run), #19 (proxy drops TC/audio), #25 (editorial fields), #26 (bit depth), #27 (VFR), #28 (skip-existing), #29 (sidecar noise).

---

## 19. Open issues — v0.3 candidates

The following issues are acknowledged and targeted for v0.3. Each requires a spec change or design decision before implementation.

### #8 — VFR causes audio sync drift in proxies

**Severity:** Real bug.
**Recommendation:** Add `-fps_mode cfr` to all proxy generation (forcing constant frame rate from variable-frame-rate sources). This normalizes all proxies to CFR regardless of source — safe because proxies are throwaway edit media. Keep `is_vfr` in the probe for visibility; the proxy command just normalizes.
**Versioning impact:** None (behavior change is a strict improvement).
**Status:** Worth doing now.

### #11 — Same-volume I/O and no parallelism

**Severity:** Partial.
**Recommendation:** Two parts:
1. **Hardlink on same device** — when source and dest are on the same volume, use `os.link()` instead of `shutil.copy2()`. Zero I/O overhead, originals stay immutable. Implemented in organize. This is the 80% solution.
2. **Device-aware parallelism** — closed as won't-fix for v0.3. Adds significant complexity for marginal gain on a single-operator CLI.
**Versioning impact:** None (hardlink is an optimization, not a behavior change).
**Status:** Hardlink: worth doing now. Parallelism: wont-fix.

### #20 — Resolve: empty project

**Severity:** Spec violation (promised in §5.4).
**Recommendation:** Implement `media_pool.ImportMedia()` and `CreateTimelineFromClips()` via the live Resolve API, or demote the capability to "manifest-first" and make the manifest the primary deliverable. The manifest layer is solid; the live-API path produces hollow projects.
**Versioning impact:** Yes — this is the headline Resolve feature.
**Status:** Worth doing now, but requires either Resolve API access for testing or a clear decision to demote to manifest-only.

### #21 — Verify silently masks corruption via rolling baseline

**Severity:** Serious — undermines the core integrity promise.
**Recommendation:** Split "snapshot" (write baseline) from "verify" (compare, never mutate on mismatch). Currently `replace_verification_snapshot()` is called unconditionally, so a corrupted file's checksum overwrites the good baseline and future runs report clean. With this fix, corruption stays flagged until explicitly acknowledged with `--accept-changes`.
**Versioning impact:** None — this is fixing a silent correctness bug, not changing advertised behavior.
**Status:** Worth doing now (one-line change in `verify.py`).

### #22 — Organize-by-codec fights AE/DIT mental model

**Severity:** Real UX problem.
**Recommendation:** Change the default organize template from `by_codec` to source-structure-preserving (e.g., `{root}/{date}/{filename}{ext}` or simply mirror source folder under dest). The template system already supports this — it's a default-value change. Codec/resolution remain available as opt-in templates.
**Versioning impact:** Yes — changes the default output layout. Existing configs using explicit templates are unaffected.
**Status:** Worth doing now (default change, minimal code).

### #23 — Spanned and multi-file clips split

**Severity:** Real.
**Recommendation:** Two parts:
1. **Keep groups together** — when a multi-file clip is detected (by naming convention), never split the group across organize destinations. Emit a warning so the user knows. This is largely free if #22's source-preserving default is implemented.
2. **Full spanned-clip model** — v1.0 candidate. Requires a logical clip abstraction that tracks group membership through organize/proxy/resolve. Retrofitting this into the current per-file model is a significant architecture change.
**Versioning impact:** Partial (warning is none; full model is breaking).
**Status:** Warning: worth doing now. Full model: v1.0.

### #24 — R3D/BRAW/ARI fail with stock ffmpeg

**Severity:** Truth-in-advertising bug.
**Recommendation:** Add a pre-check that detects `.r3d`, `.braw`, and `.ari` extensions and emits a clear error: *"R3D decode requires the RED SDK; not supported by stock ffmpeg."* Correct the README and SPEC.md §5.3 to say "container recognized; decode requires vendor plugins." Tiny fix, high credibility value.
**Versioning impact:** None (spec correction + error message).
**Status:** Worth doing now.

### #30 — Manifest and live-API bin structure mismatch

**Severity:** Real inconsistency.
**Recommendation:** Fold into #20. When implementing the live-API import path, fix `_build_bin_tree` to produce nested folder structures that match the manifest's `resolve_bin_structure`. If recursive bins are not yet supported, deliberately flatten both paths to first-level bins for consistency.
**Versioning impact:** Folded into #20.
**Status:** Worth doing, folded into #20.

---

## 20. What you sign off on by approving this doc

All items below were approved at spec time and shipped in v0.1.0:

- Scope as defined in §5
- Non-goals as defined in §4
- Architecture as sketched in §6
- Data model as sketched in §7
- Tech stack as defined in §9
- Safety constraints in §11
- Versioning rule in §12 (MAJOR=0 until D approves)
- License: MIT
- Repo location: `dspury/media-mate`
- Name: `media-mate`
- First tagged version: 0.1.0

All items below were approved and shipped in v0.2.2:

- Bug fixes in §18
- New capabilities in §18
- Data model additions in §18
- v0.3 candidates as documented in §19

Shipped as approved ✓
