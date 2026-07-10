# media-mate

[![Version](https://img.shields.io/badge/version-0.2.2-blue)](https://github.com/dspury/media-mate)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/dspury/media-mate/ci.yml?style=flat-square)](https://github.com/dspury/media-mate/actions/workflows/ci.yml)

> Zero-cost CLI for post-production media ops: probe, organize, generate proxies, build DaVinci Resolve projects, and verify backups.

media-mate is a sharp little tool for the boring-but-critical infrastructure underneath video post-production. Every operation writes to a local SQLite audit log so you can answer *"what happened to my media?"* without guessing.

- **Probe** any media file or folder → structured metadata (codec, resolution, audio, color)
- **Organize** a folder into a structured layout by codec family and resolution bucket
- **Generate** ProRes 422 Proxy (or any ProRes variant) for editing
- **Create** DaVinci Resolve projects programmatically — or fall back to a manifest if Resolve isn't available
- **Verify** folder integrity with fast checksums (xxhash or sha256) — designed for cron
- **Orchestrate** all of the above in one command with the `run` pipeline

No API keys. No cloud. No SaaS. Just FFmpeg, your local SQLite, and (optionally) DaVinci Resolve.

---

## See it in action

```
$ media-mate run ./raw/ --organize --proxy --resolve-project --verify --project-name "Episode-12"

Step 1: probe
  Probed 4 file(s)
Step 2: organize
  Copied 4, skipped 0
Step 3: proxy
  Generated 4 proxy file(s)
Step 4: resolve-project
  Created Resolve project (v20.0)
Step 5: verify
  Clean: 4 file(s) verified

Done.
```

A full walkthrough with screenshots, folder layouts, and audit-log output is in [`examples/WALKTHROUGH.md`](./examples/WALKTHROUGH.md).

---

## Why media-mate?

Most post-production tooling is either (a) expensive SaaS, (b) manual point-and-click workflows that don't scale, or (c) one-off scripts glued together. There's no widely-adopted open-source tool that handles the boring-but-critical media-ops layer — probe → organize → proxy → Resolve project → verify — in one composable, logged, reproducible package.

media-mate is built for the operator who runs a small-to-medium video team, doesn't want to write the same five scripts every project, and needs every operation to be auditable.

---

## Features

| Command | What it does |
|---|---|
| `probe` | Run `ffprobe` on every file in a folder; capture codec, resolution, frame rate, color space, audio, duration, size, mtime. |
| `organize` | Re-arrange files into a structured layout (default: `<root>/<codec_family>/<resolution_bucket>/<filename>`) based on probe data. |
| `proxy` | Generate ProRes 422 Proxy (or any ProRes variant) at 1080p via `ffmpeg`. Aspect-preserving; always outputs `.mov`, skips non-video files. |
| `resolve create` | Programmatically create a DaVinci Resolve project. Falls back to a JSON manifest when Resolve isn't available. |
| `verify` | Compute checksums for every file in a folder; on subsequent runs, report what changed (added / modified / missing) with structured exit codes. |
| `log` | Query the audit log: recent runs, with text or JSON output. |
| `run` | Pipeline orchestration: probe (always) + optional organize/proxy/resolve-project/verify. |
| `tui` | Full-screen interactive TUI — alternative to subcommands; animated progress, live log, log browser. |

Every command writes to the audit log so you can trace any operation back to what the filesystem looked like at the time.

---

## Installation

### Requirements

- **Python 3.11+** (for `tomllib` stdlib)
- **FFmpeg** with `ffprobe` on `$PATH` (or pointed at via config)
- **DaVinci Resolve Studio** (free tier is fine) — only required for the `resolve create` command; everything else works without it

### Install from source

```bash
git clone https://github.com/dspury/media-mate.git
cd media-mate
pip install -e ".[dev]"

media-mate --version
```

### Install via pip

```bash
pip install media-mate
media-mate --version
```

### Verify the install

```bash
media-mate --help
media-mate probe --help
```

If `media-mate` isn't on your PATH after pip install, check `python -m site --user-site` and make sure it's on PATH, or use `pipx install media-mate` for an isolated install.

---

## Usage

### Quick start — try it now

The [`examples/`](./examples/) folder contains a minimal test dataset and a run script so you can see media-mate working immediately, no real media files required.

```bash
cd examples/
./run-demo.sh
```

### `probe`

```bash
# Probe a single file
media-mate probe clip.mov

# Probe a folder recursively
media-mate probe ./raw/
```

Output:

```
Probed 5 file(s)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ File                      ┃ Codec ┃ Resolution  ┃ Duration ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━┩
│ raw/clip.mov              │ h264  │ 1920x1080  │ 60.0s   │
│ raw/sub/b-roll.mov        │ prores│ 1920x1080  │ 32.5s   │
│ ...                       │       │            │          │
└───────────────────────────┴───────┴────────────┴──────────┘
```

### `organize`

```bash
media-mate organize ./raw/ --root ./organized/
```

Requires that the files have already been probed. Files without probe data are skipped (with a clear error message); run `media-mate probe` first.

Sources are **copied** by default — the raw folder is treated as immutable camera media. Pass `--move` to relocate instead.

Default layout: `<root>/<codec_family>/<resolution_bucket>/<filename>`. Customize via `media-mate.toml`.

### `proxy`

```bash
# Defaults: ProRes 422 Proxy at 1080p
media-mate proxy ./raw/ --out ./proxies/

# Custom codec + height (configured via media-mate.toml)
```

The output directory mirrors the source's subpath layout. If a proxy already exists at the output path, it's skipped (no overwrite).

### `resolve create`

```bash
# Full create with all options
media-mate resolve create ./raw/ \
    --project "Episode-12" \
    --resolution 1080 \
    --fps 24 \
    --color-space Rec.709 \
    --proxy-dir ./proxies/ \
    --output ~/projects/Episode-12.drp
```

If DaVinci Resolve is running and the scripting API is available, the project is created live. If not, a manifest file (`Episode-12.drp.manifest.json`) is written describing what would be created — you can manually build the project in Resolve from this manifest.

### `verify`

```bash
# First run: creates a snapshot
media-mate verify ./backup/

# Subsequent runs: report what changed since the previous verify
media-mate verify ./backup/
echo $?  # 0 = clean, 1 = missing, 2 = modified, 3 = added
```

Designed for cron:

```cron
# Every night at 3am, verify the backup drive and alert on any change
0 3 * * * cd /path/to/workspace && media-mate verify /Volumes/Backup/ || mail -s "Backup alert" me@example.com
```

### `log`

```bash
# Recent runs (text table)
media-mate log

# Recent runs as JSON
media-mate log --format json

# Limit count
media-mate log --limit 5
```

### `run` (pipeline)

```bash
# Probe only
media-mate run ./raw/

# Probe + organize + proxy + resolve-project + verify
media-mate run ./raw/ \
    --organize \
    --proxy \
    --resolve-project \
    --verify \
    --project-name "Episode-12"
```

Step order is fixed: probe (always) → organize → proxy → resolve-project → verify. Each step is independent; skip any combination you don't need.

### `tui` (interactive TUI)

For ad-hoc use, launch the full-screen Textual TUI instead of typing subcommands:

```bash
media-mate tui
```

The TUI has four screens:

- **Home** — system status: ffmpeg version, db location, run counts
- **Pipeline** — enter a folder path, toggle which steps to run, watch animated progress with a live log
- **Log** — browse the audit log in a sortable table, color-coded by status
- **Settings** — view current config: proxy codec/height, checksum algo, Resolve path

All four screens are also reachable via keyboard shortcut or mouse from the home menu.

---

## Configuration

Optional. media-mate works with defaults out of the box.

`media-mate.toml` is searched in this order:
1. `--config <path>` argument or `MEDIA_MATE_CONFIG` env var
2. `./media-mate.toml` in the current directory
3. `~/.media-mate/config.toml`

Example `media-mate.toml`:

```toml
# Where to find the ffmpeg and ffprobe binaries. If unset, looks on PATH.
# ffmpeg_path = "/opt/homebrew/bin/ffmpeg"
# resolve_path = "/Applications/DaVinci Resolve.app"

[organize]
template = "{root}/{codec_family}/{resolution_bucket}/{filename}{ext}"
on_conflict = "skip"  # skip | overwrite | rename

proxy_codec = "ProRes422Proxy"
proxy_height = 1080

# Options: xxhash (default, ~10x faster) | sha256
checksum_algo = "xxhash"
```

See [`media-mate.toml.example`](./media-mate.toml.example) for the full reference.

---

## The audit log

Every operation writes to `~/.media-mate/media-mate.db` (SQLite). The schema covers runs, files, probes, proxies, projects, verifications, and organize operations.

The audit log answers questions like:

- "When did this file get probed, and what was the result?"
- "What got copied during the last organize run?"
- "When was this Resolve project created, and from what source folder?"
- "What was the checksum of this file at the last verify?"

The log is the system of record — you can back it up, copy it between machines, and trust it as the source of truth.

---

## Architecture

See [`SPEC.md`](./SPEC.md) for the full write-up: goals, capabilities, data flow, SQLite schema, and how to extend.

**Stale archived docs** (superseded by SPEC.md) are kept at [`docs/archive/`](./docs/archive/) but excluded from version control via `.gitignore`.

---

## Status

**Beta (`0.2.1`).** Versioned per the project's beta scheme: `MAJOR.MINOR.PATCH` where `MAJOR` stays at `0` indefinitely. Patch bumps (0.1.3 → 0.1.4) are autonomous; minor bumps (0.1.3 → 0.2.0) require explicit approval. We do not bump to `1.0.0` without the maintainer's say-so.

What works in `0.2.1`:
- All six core capabilities (probe, organize, proxy, resolve, verify, run/log)
- Interactive Textual TUI (`media-mate tui`) — home, pipeline runner, log browser, settings
- Local SQLite audit log with full schema
- Click CLI with --help, --version, --db, --config
- media-mate.toml configuration with full defaults
- xxhash and sha256 checksum algorithms
- Graceful Resolve-API fallback to manifest file
- 259 tests passing; ruff + mypy strict clean

What's planned for future versions:
- Scene detection (PySceneDetect)
- Audio loudness analysis
- Watch-folder mode
- Web UI
- Cloud-storage adapters

---

## Contributing

media-mate is open source under the MIT license. Contributions welcome — open an issue or PR on [GitHub](https://github.com/dspury/media-mate).

Development setup:

```bash
git clone https://github.com/dspury/media-mate.git
cd media-mate
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint + type-check
ruff check .
ruff format --check .
mypy src
```

---

## License

MIT — see [`LICENSE`](./LICENSE).

---

media-mate is part of the media-mate catalog of free open-source tools. It's designed to be useful on its own and composes nicely with other tools in the catalog.
