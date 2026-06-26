# media-mate

[![Version](https://img.shields.io/badge/version-0.1.0-blue)](https://github.com/dspury/relay-dept-products/tree/main/products/media-mate)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![CI](https://github.com/dspury/relay-dept-products/actions/workflows/media-mate.yml/badge.svg)](https://github.com/dspury/relay-dept-products/actions/workflows/media-mate.yml)

> Zero-cost CLI for post-production media ops: probe, organize, generate proxies, build DaVinci Resolve projects, and verify backups.

media-mate is a sharp little tool for the boring-but-critical infrastructure underneath video post-production. Every operation writes to a local SQLite audit log so you can answer "what happened to my media?" without guessing.

- **Probe** any media file or folder → structured metadata (codec, resolution, audio, color)
- **Organize** a folder into a structured layout by codec family and resolution bucket
- **Generate** ProRes 422 Proxy (or any ProRes variant) for editing
- **Create** DaVinci Resolve projects programmatically — or fall back to a manifest if Resolve isn't available
- **Verify** folder integrity with fast checksums (xxhash or sha256) — designed for cron

No API keys. No cloud. No SaaS. Just FFmpeg, your local SQLite, and (optionally) DaVinci Resolve.

---

## Quickstart

```bash
# Install (requires Python 3.11+, FFmpeg on PATH)
pip install media-mate

# Probe a folder of media
media-mate probe ./raw/

# Organize + generate proxies + create a Resolve project + verify
media-mate run ./raw/ --organize --proxy --resolve-project --verify \
    --project-name "Episode-12"

# Query the audit log
media-mate log
```

That's it. Every command writes to a local SQLite database at `~/.media-mate/media-mate.db`.

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
| `proxy` | Generate ProRes 422 Proxy (or any ProRes variant) at 1080p via `ffmpeg`. Aspect-preserving. |
| `resolve create` | Programmatically create a DaVinci Resolve project. Falls back to a JSON manifest when Resolve isn't available. |
| `verify` | Compute checksums for every file in a folder; on subsequent runs, report what changed (added / modified / missing) with structured exit codes. |
| `log` | Query the audit log: recent runs, with text or JSON output. |
| `run` | Pipeline orchestration: probe (always) + optional organize/proxy/resolve-project/verify. |

Every command writes to the audit log so you can trace any operation back to what the filesystem looked like at the time.

---

## Installation

### Requirements

- **Python 3.11+** (for `tomllib` stdlib)
- **FFmpeg** with `ffprobe` on `$PATH` (or pointed at via config)
- **DaVinci Resolve Studio** (free tier is fine) — only required for the `resolve create` command; everything else works without it

### Install from PyPI

```bash
pip install media-mate
media-mate --version
```

### Install from source

```bash
git clone https://github.com/dspury/relay-dept-products.git
cd relay-dept-products/products/media-mate
pip install -e ".[dev]"
```

### Verify the install

```bash
media-mate --help
media-mate --version
```

If `media-mate` isn't on your PATH after pip install, check `python -m site --user-site` and make sure it's on PATH, or use `pipx install media-mate` for an isolated install.

---

## Usage

### `probe`

```bash
# Probe a single file
media-mate probe clip.mov

# Probe a folder recursively
media-mate probe ./raw/
```

Output (truncated for clarity):

```
Probed 5 file(s)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ File                              ┃ Codec  ┃ Resolution   ┃ Duration ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ /Users/me/raw/clip.mov            │ h264   │ 1920x1080    │ 60.0s    │
│ /Users/me/raw/sub2/b-roll.mov     │ prores │ 1920x1080    │ 32.5s    │
│ ...                               │        │              │          │
└───────────────────────────────────┴───────┴──────────────┴──────────┘
```

### `organize`

```bash
media-mate organize ./raw/ --root ./organized/
```

Requires that the files have already been probed. Files without probe data are skipped (with a clear error message); run `media-mate probe` first.

Default layout: `<root>/<codec_family>/<resolution_bucket>/<filename>`. Customize via `media-mate.toml`.

### `proxy`

```bash
# Defaults: ProRes 422 Proxy at 1080p
media-mate proxy ./raw/ --out ./proxies/

# Custom codec + height
# (configured via media-mate.toml)
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
- "What got moved during the last organize run?"
- "When was this Resolve project created, and from what source folder?"
- "What was the checksum of this file at the last verify?"

The log is the system of record — you can back it up, copy it between machines, and trust it as the source of truth.

---

## Troubleshooting

**`ffmpeg not found` / `ffprobe not found`**
Install FFmpeg: `brew install ffmpeg` (macOS), `apt-get install ffmpeg` (Debian/Ubuntu), or set `ffmpeg_path` in `media-mate.toml`.

**`media-mate resolve create` writes a manifest instead of creating the project**
Resolve isn't running or the scripting API isn't on `PYTHONPATH`. See [DaVinci Resolve scripting docs](https://www.blackmagicdesign.com/developer/product/davinci-resolve) — set `resolve_path` in `media-mate.toml` to your Resolve installation root.

**Permission denied on the audit log**
By default the DB lives at `~/.media-mate/media-mate.db`. Make sure that directory is writable, or pass `--db <other-path>`.

---

## Architecture

See [`docs/architecture.md`](./docs/architecture.md) for the full architecture write-up: capabilities, data flow, SQLite schema, and how to extend.

---

## Status

**Beta (`0.1.0`).** Versioned per the project's beta scheme: `MAJOR.MINOR.PATCH` where `MAJOR` stays at `0` indefinitely. Patch bumps (0.1.0 → 0.1.1) are autonomous; minor bumps (0.1.0 → 0.2.0) require explicit approval. We do not bump to `1.0.0` without the maintainer's say-so.

What works in `0.1.0`:
- All six core capabilities (probe, organize, proxy, resolve, verify, run/log)
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

media-mate is open source under the MIT license. Contributions welcome — open an issue or PR on [GitHub](https://github.com/dspury/relay-dept-products/tree/main/products/media-mate).

Development setup:

```bash
git clone https://github.com/dspury/relay-dept-products.git
cd relay-dept-products/products/media-mate
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

## Acknowledgments

media-mate is part of the [relay-dept-products](https://github.com/dspury/relay-dept-products) catalog of free open-source tools. It's designed to be useful on its own and composes nicely with other tools in the catalog.