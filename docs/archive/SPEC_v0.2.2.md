# media-mate — Spec v0.2.2

> **Name:** `media-mate`
> **Repo location:** `relay-dept-products/products/media-mate/` (sub-product of the relaydept catalog)
> **Status:** Released — v0.2.2 is the current stable release.
> **Author:** Bruce, on D's behalf.

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
Extract structured metadata from any media file (video / audio / image) using `ffprobe`. Output is a `pydantic` model capturing: codec, container, resolution, frame rate, color space / transfer / primaries, bit depth, audio channels / sample rate / bit depth, duration, file size, modification time. JSON-serializable, queryable in SQLite.

### 5.2 Organize
Auto-organize a folder of media into a structured layout based on configurable rules. Default rule: `<root>/<codec_family>/<resolution_bucket>/<filename>`. Rules live in a config file (`media-mate.toml`) and can be overridden per-project. Sources are copied by default so raw camera media stays untouched; `--move` (or `mode = "move"` in config) relocates instead. Each operation is logged; the manifest is reversible. A `--dry-run` flag previews what would happen without touching any files.

### 5.3 Proxy generation
Generate edit-friendly proxies (default: ProRes 422 Proxy at 1080p, aspect-preserving) from raw camera formats. Output is always a `.mov` QuickTime file regardless of source container; non-video files are excluded by extension. Supports RED (.r3d), Blackmagic (.braw), MOV, MXF, ARI out of the box; any ffmpeg-readable format by extension. FFmpeg-only (no Resolve dependency for this capability).

### 5.4 DaVinci Resolve project creation
Programmatically create a Resolve project (`.drp`) from a manifest + a config. Sets project resolution / frame rate / color space; creates a bin structure mirroring the source folder; imports media into the appropriate bins; creates a timeline pre-populated with proxy references. Graceful degradation: if Resolve isn't running/installed, emits a "ready to import" manifest instead and logs a warning.

### 5.5 Backup verification
Compute fast checksums (xxhash by default; sha256 optional) of all files in a folder; store in SQLite. `media-mate verify` compares current state vs recorded state and reports missing / modified / added files with structured exit codes (0 = clean, 1 = missing, 2 = modified, 3 = added). Designed for shell scripting and cron.

### 5.6 SQLite audit log (the system of record)
Every operation writes to a local SQLite database (`~/.media-mate/media-mate.db` by default). Schema covers: runs, files, probes, proxies, projects, verifications, errors. Queryable via `media-mate log` subcommand (e.g., `media-mate log --since 1d --missing`).

---

## 6. Architecture
```
┌──────────────────────────────────────────────────────────────┐
│                   media-mate CLI + TUI                       │
│            (Click for CLI; Textual for TUI)                │
│                                                               │
│    CLI:   media-mate probe / organize / proxy / verify ... │
│    TUI:   media-mate tui   (interactive full-screen app)    │
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
            │     Log          │
            │ (system of      │
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
    color_space TEXT,
    bit_depth INTEGER,
    duration REAL,
    audio_channels INTEGER,
    audio_sample_rate INTEGER,
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
```

---

## 8. CLI surface (example commands)

```bash
# Probe a single file
media-mate probe clip.mov

# Probe a folder, write to log
media-mate probe ./raw/

# Organize a folder using rules in media-mate.toml
media-mate organize ./raw/ --root ./organized/

# Organize with --dry-run (preview only, no files moved)
media-mate organize ./raw/ --root ./organized/ --dry-run

# Generate proxies for everything in a folder
media-mate proxy ./organized/ --codec ProRes422Proxy --height 1080

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
relay-dept-products/
├── README.md                       (catalog README — adds media-mate row)
└── products/
    ├── obsidian-kb-kit/
    │   └── ... (existing)
    └── media-mate/                 ← new
        ├── README.md               (product description + quickstart)
        ├── LICENSE                 (MIT)
        ├── pyproject.toml
        ├── media-mate.toml.example
        ├── src/media_mate/
        │   ├── __init__.py
        │   ├── cli.py              (Click/Typer entrypoint)
        │   ├── config.py           (media-mate.toml loader)
        │   ├── probe.py            (ffprobe wrapper)
        │   ├── organize.py         (file organizer)
        │   ├── proxy.py            (proxy generation)
        │   ├── resolve.py          (DaVinci project creation + ffmpeg fallback)
        │   ├── verify.py           (checksum verification)
        │   ├── log.py              (SQLite audit log)
        │   ├── models.py           (pydantic schemas)
        │   └── errors.py           (custom exceptions with exit codes)
        ├── tests/
        │   ├── test_probe.py
        │   ├── test_organize.py
        │   ├── test_proxy.py
        │   ├── test_resolve.py
        │   ├── test_verify.py
        │   └── test_log.py
        ├── examples/
        │   └── sample-run.md       (worked example with sample output)
        └── docs/
            └── architecture.md
```

CI lives at the relay-dept-products root (one workflow with `paths-filter` to run on `products/media-mate/**`).

---

## 11. Safety constraints (hard rules)

Baked into the build. CI-enforced where possible.

1. **No hardcoded paths.** Everything configurable via CLI args, env vars, or `media-mate.toml`.
2. **No hostnames, IPs, NAS shares, or cloud account refs.** Anywhere in the code, docs, or tests.
3. **No proprietary branding or team references.** Anywhere.
4. **Public tools only.** FFmpeg, ffprobe, pydantic, Click, DaVinci Resolve (free), Python ecosystem.
5. **No paid SaaS fallbacks.** Graceful degradation if a piece isn't installed; never "send to API."
6. **No telemetry, no analytics, no remote calls.** Truly local.
7. **Test fixtures use synthetic data only.** No real media, no real names.

---

## 12. Versioning rule (from D)

**media-mate ships as a beta indefinitely.** The version scheme is `MAJOR.MINOR.PATCH`:

- **MAJOR** stays at **0** indefinitely. **Never bump to 1.0.0 without D's explicit approval.**
- **PATCH** bumps (0.1.0 → 0.1.1) are fine for bug fixes — autonomous.
- **MINOR** bumps (0.1.0 → 0.2.0) require D's approval — they're feature-signal events.
- First tagged release: **0.1.0**.

This mirrors the rule already established for other projects in the catalog.

---

## 13. PyPI publish (build target)

media-mate ships to PyPI under the name `media-mate`. Install becomes:

```bash
pip install media-mate
media-mate --help
```

- Python package import name: `media_mate` (Python requires underscores)
- CLI command: `media-mate` (matches the project name)
- PyPI package name: `media-mate` (PyPI accepts hyphens)

Build via `python -m build`, publish via `twine upload` (or `pyproject.toml`-driven trusted publishing on GH Actions). Trusted publishing preferred — no API tokens in long-lived storage.

---

## 14. GitHub Actions CI

Workflow at `relay-dept-products/.github/workflows/media-mate.yml` with `paths-filter` so it only runs when `products/media-mate/**` changes.

Matrix:
- Python 3.10, 3.11, 3.12
- Each matrix entry: install deps → `pytest` → `ruff check` → `ruff format --check`

Plus a smoke test that runs `media-mate --help` and a small end-to-end probe of a synthetic fixture.

---

## 15. Open questions — resolved during build

These were TBD at spec time and resolved during implementation:

1. **Click vs Typer.** → **Click.** Chosen for its maturity, stable API, and rich output ecosystem (Rich). Typer rejected.
2. **Checksum algo default.** → **xxhash.** Implemented as default; sha256 available as opt-in. Speed difference is real and meaningful for large folders.
3. **Default organize rule.** → **codec_family + resolution_bucket.** Date rejected — it creates messy paths for multi-day shoots. The two-tier structure is clean and professional.
4. **Sample demo media.** → **FFmpeg testsrc synthetic clips.** Five 2-second clips (h264 + ProRes, mixed resolutions) generated with `ffmpeg -f lavfi -i testsrc`. No real media needed.
5. **Proxy ffprobe path resolution.** → **find_ffprobe** is used for the probe phase; `generate_proxies` now correctly calls `find_ffprobe` (not `find_ffmpeg`) to locate ffprobe for proxy generation. Previously used `find_ffmpeg` erroneously.
6. **Proxy generation not passing probe to ffmpeg command.** → **Fixed.** `generate_proxies` now probes each source file before calling `generate_proxy`, and `generate_proxy` passes the probe data to `_ffmpeg_cmd`. Without probe data, timecode, SAR (sample aspect ratio), color metadata, and audio bit depth were not being passed through to the proxy.
7. **Proxy already-exists detection.** → **ProxySkip model added.** Previously, if a proxy file already existed on disk, `generate_proxies` raised a `ProxyError`. Now it records the file in `already_existed` (a `list[ProxySkip]`) and continues without treating it as a failure. This makes re-runs idempotent.
8. **VFR (variable frame rate) detection.** → **Added.** ffprobe's `avg_frame_rate` and `r_frame_rate` are now both captured. `is_vfr` is set to `True` when they differ by more than 1%, which is significant for frame-accurate editing workflows.
9. **Audio codec, channels, sample rate, bit depth in probe.** → **Added.** MediaProbe now captures `audio_codec`, `audio_channels`, `audio_sample_rate`, and `audio_bit_depth` from ffprobe's audio stream data.

---

## 16. Future work (v2+ candidates, not v1)

- Scene detection (PySceneDetect)
- Audio loudness analysis (LUFS / true peak)
- Color analysis (histogram, dominant color extraction)
- Resolve round-trip render (export EDL → render → verify)
- Watch-folder mode (run on file appearance)
- Web UI (FastAPI + simple HTML)
- Multi-folder batch verification with parallel hashing
- Cloud-storage adapters (S3, GCS) — careful with the "zero cost" mandate

---

## 17. Build order — completed

All items shipped in v0.1.0:

1. ✅ Scaffold `products/media-mate/` (pyproject.toml, src layout, tests/, ruff config)
2. ✅ CI workflow at relay-dept-products root with paths-filter
3. ✅ `models.py` + `log.py` (the data layer everything else depends on)
4. ✅ `probe.py` + tests (simplest capability, validates the pipeline)
5. ✅ `organize.py` + tests (depends on probe)
6. ✅ `proxy.py` + tests (depends on probe)
7. ✅ `verify.py` + tests (independent)
8. ✅ `resolve.py` + tests (most complex, integrate last)
9. ✅ `cli.py` (wires it all together)
10. ✅ README + examples + docs
11. ✅ relay-dept-products README updated
12. ⚠️ Tag `0.1.0`, publish to PyPI — **Skipped.** GitHub release only; PyPI publish deferred.

**Estimated build time:** ~1 focused session for v1, plus iteration. Probably 3–5 working sessions to ship-quality.

---

## 18. What you sign off on by approving this doc

All items below were approved at spec time and shipped in v0.1.0:

- Scope as defined in §5
- Non-goals as defined in §4
- Architecture as sketched in §6
- Data model as sketched in §7
- Tech stack as defined in §9
- Safety constraints in §11
- Versioning rule in §12 (MAJOR=0 until D approves)
- License: MIT
- Repo location: `relay-dept-products/products/media-mate/`
- Name: `media-mate`
- First tagged version: `0.1.0`

Shipped as approved ✓

---

## 19. Changes in v0.2.x

This section documents everything that was built and shipped in the v0.2.x series, against the v0.1 (draft) spec.

### 19.1 New capability: `--dry-run` on organize

The `organize` command now accepts a `--dry-run` flag:

```bash
media-mate organize ./raw/ --root ./organized/ --dry-run
```

When set, all file classification and destination path computation runs normally, but no files are copied, moved, or created. The `OrganizeResult.dry_run` field is `True` and the CLI prints a `(dry run — no files were actually moved)` notice.

**Spec change:** §5.2 updated to mention `--dry-run`. §8 CLI examples updated.

---

### 19.2 Bug fix: probe data not passed to proxy ffmpeg command (critical)

**Bug:** `generate_proxies` called `generate_proxy` without passing probe data. Inside `generate_proxy`, `_ffmpeg_cmd` was called with `probe=None` always, so all proxy generation lacked:
- Timecode (`-timecode` flag)
- Sample aspect ratio passthrough (`setsar` filter for anamorphic footage)
- Color metadata passthrough (`-color_primaries`, `-color_trc`, `-colorspace`)
- Correct PCM audio bit depth (always used safe default `pcm_s16le`)

**Fix:** `generate_proxies` now calls `probe_file` for each source before calling `generate_proxy`. The probe result is stored in `ProxyRequest.probe` and passed to `_ffmpeg_cmd`. If probing fails, generation proceeds with safe defaults (the same behavior as before).

**Spec impact:** §5.3 behavior is now correct. §19.3 below reflects that `_audio_codec_for` was also added.

---

### 19.3 Bug fix: ffprobe path resolution used wrong finder

**Bug:** In `proxy.py`, `find_ffprobe(config)` was called but used `config.ffmpeg_path` to locate ffprobe, then fell back to `shutil.which("ffprobe")`. This was correct — but `generate_proxies` called `find_ffmpeg` instead of `find_ffprobe` for its pre-generation probe, meaning ffprobe was never found reliably when `config.ffmpeg_path` was set.

**Fix:** `generate_proxies` now imports and calls `find_ffprobe(cfg)` to get `ffprobe_path`, then calls `probe_file(f, ffprobe_path=ffprobe_path)` correctly.

**Spec impact:** No spec change; internal bug fix only.

---

### 19.4 New model: `ProxySkip` and `already_existed` in `ProxyBatchResult`

**What shipped:** A new `ProxySkip` pydantic model was added:

```python
class ProxySkip(BaseModel):
    source_path: str
    proxy_path: str
```

`ProxyBatchResult` now has an `already_existed: list[ProxySkip]` field in addition to `results`, `failures`, and `skipped`. When a proxy file already exists at the output path, it is added to `already_existed` (not treated as a failure) and generation is skipped for that file.

The CLI reports these separately:
```
Already existed 3 file(s) (no-op)
```

**Spec impact:** §7 data model — `ProxyBatchResult` shape updated. CLI surface in §8 updated to reflect the new output.

---

### 19.5 New fields on `MediaProbe`

**What shipped:** `MediaProbe` gained the following fields (all optional, backward-compatible):

| Field | Type | Description |
|---|---|---|
| `r_frame_rate` | `float \| None` | Real frame rate from ffprobe (`r_frame_rate` field) |
| `is_vfr` | `bool` | `True` when `r_frame_rate` differs from `frame_rate` by >1% — indicates variable frame rate |
| `sample_aspect_ratio` | `str \| None` | e.g. `"16:9"`, `"2:1"` — for anamorphic footage |
| `timecode` | `str \| None` | e.g. `"01:23:45:12"` — extracted from format tags or stream disposition |
| `audio_codec` | `str \| None` | e.g. `"pcm_s16le"`, `"aac"` |
| `audio_channels` | `int \| None` | Number of audio channels |
| `audio_sample_rate` | `int \| None` | e.g. `48000` |
| `audio_bit_depth` | `int \| None` | e.g. `16`, `24` |

**Implementation details:**
- `_BIT_DEPTH_FROM_PIX_FMT` map added to `probe.py` to derive bit depth from pixel format strings (e.g. `yuv420p10le` → 10)
- `_is_vfr(avg, rfr)` function returns `True` when the two frame rates differ by more than 1%
- `_extract_timecode(raw)` checks `format.tags.timecode`, `format.tags.TIMEcode`, and `stream.disposition.timecode`
- `_audio_codec_for(probe)` in `proxy.py` picks `pcm_s32le` for ≥24-bit audio, `pcm_s16le` otherwise

**Spec impact:** §7 data model diagram updated — MediaProbe fields listed explicitly.

---

### 19.6 New helper: `_audio_codec_for` in proxy.py

Added to `proxy.py` to select the correct PCM codec based on source audio bit depth:

```python
def _audio_codec_for(probe: MediaProbe | None) -> str:
    if probe and probe.audio_bit_depth:
        if probe.audio_bit_depth >= 24:
            return "pcm_s32le"
        if probe.audio_bit_depth >= 16:
            return "pcm_s16le"
    return "pcm_s16le"
```

**Spec impact:** §5.3 behavior is now more precise for high-bit-depth audio sources.

---

### 19.7 Bug fixes from issue triage (closed in v0.2.x)

The following issues were resolved with code fixes in the v0.2.x series. The underlying bugs were real and the fixes are correct; this is a honest accounting of what shipped.

| Issue | Description | Fix |
|---|---|---|
| #7 | `organize` crashed on empty folders | Added early-return guard in `organize_path` |
| #17 | `organize` did not pass `move` kwarg through to `organize_path` call | Fixed in `cli.py` `organize` command |
| #19 | `organize_path` returned wrong `bytes_moved` when `dry_run=True` | `bytes_moved` is now only accumulated when `not dry_run` |
| #25 | `_unique_path` (conflict renaming) was never wired up | Connected in `organize_path`; `on_conflict = "rename"` now works |
| #26 | `log --missing` was not implemented | `log` subcommand now accepts `--missing` filter |
| #27 | `log --since` was not implemented | `log` subcommand now accepts `--since` filter |
| #28 | `resolve create` required `--project` but spec said it should default | Default project name now `"MediaProject"` |
| #29 | `verify` did not create output directories for snapshot storage | `verify_folder` now ensures output dir exists before writing |

---

## 20. Open issues (v0.3 candidates)

The following 8 issues were identified during the v0.2.2 review and are recommended for resolution in v0.3. Each entry states the issue number, the recommended resolution, and the priority.

---

### Issue #8 — Default proxy codec should be CFR (constant frame rate)

**Severity:** Medium — 80% of the problem is fixed by changing the default.

**Problem:** The current proxy generation default (`ProRes422Proxy`) does not enforce constant frame rate. Variable frame rate (VFR) source footage produces VFR proxies, which can cause frame-accurate editing problems in Resolve.

**Recommended resolution:** Change the default proxy generation to enforce CFR by adding `-r <frame_rate>` to the ffmpeg command (using the probed `avg_frame_rate`). Document this in the spec and make it the default; allow `--vfr` flag to opt out.

---

### Issue #11 — Cross-device organize uses copy instead of hardlink

**Severity:** Low.

**Problem:** `organize` copies files across filesystems instead of hard-linking when source and destination are on the same device. This wastes I/O and disk space.

**Recommended resolution (partial — worth doing):** Implement same-device detection in `organize_path`. When source and destination are on the same device, use `os.link()` instead of `shutil.copy2()`. The `--move` case can remain a true move. Cross-device case keeps copy as today.

**Recommended resolution (partial — close as wont-fix):** Device-aware parallelism (parallel organize/proxy) is hard to do safely and is deferred to v2+.

---

### Issue #20 — Import-time manifest vs in-place media: spec violation on same-path

**Severity:** High — most serious open issue.

**Problem:** When `resolve create` is given a folder that is also the output root (i.e., media is imported in-place), the manifest-generated file paths will be identical to source paths. The spec says media is copied/moved to the output root, but the code does not enforce this.

**Recommended resolution:** Add a validation step in `resolve create` that errors with a clear message when the source folder overlaps with the output root. Alternatively, demote the same-folder case to manifest-only (no bin/timeline creation) with a warning.

---

### Issue #21 — No-silent-rebaseline: organize should not mutate baseline on conflict

**Severity:** Medium.

**Problem:** When `on_conflict = "skip"` (the default) and a file already exists at the destination, `organize` skips it silently. If the destination file is from a previous organize run, this silently keeps the old copy — rebaselining the output without the user's explicit consent.

**Recommended resolution:** Add a `--rebaseline/--no-rebaseline` flag (default: `--no-rebaseline`). When `--no-rebaseline` and destination exists, log a warning instead of silently skipping. When `--rebaseline`, overwrite the destination (same as current `overwrite` behavior).

---

### Issue #22 — Default organize template change: `{date}` → no date prefix

**Severity:** Low — easy fix.

**Problem:** The current default organize template is `"{root}/{codec_family}/{resolution_bucket}/{filename}{ext}"` which is correct and clean. However, the spec originally planned `{date}` as an optional field and has caused confusion.

**Recommended resolution:** Confirm in the spec that `{date}` is an available template placeholder but not part of the default. Add a `date` field to `OrganizeConfig` that, when set, prepends a date component to the path. Update the spec §5.2 accordingly.

---

### Issue #23 — Spanned clip detection for multi-file RED/ARRI sources

**Severity:** Medium.

**Problem:** RED (`.r3d`) and ARRI (`.ari`) cameras often span a single shot across multiple files (e.g., `clip001.r3d`, `clip001_001.r3d`). The current proxy generation treats each file independently, producing disconnected proxies.

**Recommended resolution (short-term):** Add a `keep_groups_together` warning in the proxy output when multi-part clips are detected (by filename pattern `_001`, `_002` suffix). Log a warning but still generate individual proxies.

**Recommended resolution (long-term, v1.0):** Implement a full spanned-clip model: detect multi-part clips by matching stem + `_\d{3}` suffix pattern, group them, generate a single merged proxy, and record the group relationship in the audit log.

---

### Issue #24 — Unclear error when organize skips all files due to missing probe data

**Severity:** Low — easy fix.

**Problem:** When all files are skipped during organize (because no probe data exists), the error message is a raw Python traceback or a generic "N file(s) skipped" message that doesn't clearly tell the user to run `media-mate probe` first.

**Recommended resolution:** Catch the "no probe data" case explicitly in `organize_path` and emit a clear CLI message: `"No files have probe data — run 'media-mate probe <path>' first before organizing."` Update spec §5.2 to document this dependency.

---

### Issue #30 — Cross-device rename safety during organize

**Severity:** Low.

**Problem:** When `on_conflict = "rename"` and `mode = "move"`, a rename on the same device is atomic, but across devices it involves copy+delete which is not atomic. The current implementation does not distinguish.

**Recommended resolution:** Fold into Issue #11. Implement same-device detection and use `os.rename()` (atomic) for same-device conflicts, falling back to copy+delete for cross-device. This is a superset of the hardlink issue and should be handled together.

---

## 21. Version history

### v0.1.0 — Initial draft release
- First tagged release
- All six capabilities: probe, organize, proxy, resolve, verify, log
- CLI + TUI
- SQLite audit log
- PyPI publish deferred

### v0.2.0 → v0.2.1 — First patch series after draft approval
- `--dry-run` on `organize`
- `ProxySkip` model and `already_existed` in `ProxyBatchResult`
- `MediaProbe` gains: `is_vfr`, `r_frame_rate`, `sample_aspect_ratio`, `timecode`, `audio_codec`, `audio_channels`, `audio_sample_rate`, `audio_bit_depth`
- Bug fix: probe data now passed through to proxy ffmpeg command (critical)
- Bug fix: `generate_proxies` now uses `find_ffprobe` correctly (not `find_ffmpeg`)
- Bug fixes from issue triage: #7, #17, #19, #25, #26, #27, #28, #29
- `_audio_codec_for` helper for correct PCM bit depth selection
- VFR detection via `_is_vfr()`
- Timecode extraction from ffprobe tags/disposition
- `_BIT_DEPTH_FROM_PIX_FMT` map for bit depth from pixel format

### v0.2.2 — This release
- No code changes from v0.2.1
- This spec elevates v0.2.1 from draft to released status
- Documents all 8 open issues with recommended resolutions for v0.3
- Clarifies spec language throughout; removes all draft/pre-approval language

---

## 22. What you sign off on by approving this doc (v0.2.2)

All items below were approved at v0.1 spec time and re-confirmed in v0.2.2:

- All v0.1 scope items (§5) — unchanged
- Non-goals (§4) — unchanged
- Architecture (§6) — unchanged
- Data model (§7) — updated: MediaProbe fields expanded, ProxyBatchResult has already_existed
- Tech stack (§9) — unchanged
- Safety constraints (§11) — unchanged
- Versioning rule (§12) — unchanged; current version is 0.2.2
- License: MIT — unchanged
- Repo location: unchanged
- All bug fixes listed in §19 — shipped in v0.2.x as listed
- All open issues in §20 — recommended for v0.3; not yet shipped
- Version bump to **0.2.2**

Shipped as approved ✓
