# media-mate — Spec v0.1 (DRAFT)

> **Name:** `media-mate`
> **Repo location:** `relay-dept-products/products/media-mate/` (sub-product of the relaydept catalog)
> **Status:** Awaiting D's sign-off before any code is written.
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
Auto-organize a folder of media into a structured layout based on configurable rules. Default rule: `<root>/<codec_family>/<resolution_bucket>/<filename>`. Rules live in a config file (`media-mate.toml`) and can be overridden per-project. Sources are copied by default so raw camera media stays untouched; `--move` (or `mode = "move"` in config) relocates instead. Each operation is logged; the manifest is reversible.

### 5.3 Proxy generation
Generate edit-friendly proxies (default: ProRes 422 Proxy at 1080p, aspect-preserving) from raw camera formats. Supports RED (.r3d), Blackmagic (.braw), MOV, MXF, ARI out of the box; any ffmpeg-readable format by extension. FFmpeg-only (no Resolve dependency for this capability).

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
│            (Click for CLI; Textual for TUI)                 │
│                                                               │
│    CLI:   media-mate probe / organize / proxy / verify ... │
│    TUI:   media-mate tui   (interactive full-screen app)   │
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