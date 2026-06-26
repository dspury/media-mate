# Sample Run — End-to-End Pipeline

This walkthrough takes you from a folder of raw camera files to a fully-organized, proxied, verified media library with a Resolve project on top. All in one `media-mate run` invocation.

## Starting state

Imagine you've just dumped the day's shoot into `~/raw/`:

```
~/raw/
├── A001_C001.mov       # main camera angle
├── A001_C002.mov
├── B001_C001.mov       # B-camera angle
└── sub/
    └── C001_C001.mov   # insert shot
```

These are ProRes 422 files at 1080p24. No organization, no metadata captured, no proxies yet.

## Run the full pipeline

```bash
media-mate run ~/raw/ \
    --organize \
    --proxy \
    --resolve-project \
    --verify \
    --project-name "Day-01-Shoot"
```

This single invocation:

1. **Probes** every file in `~/raw/` (writes metadata to the audit log)
2. **Organizes** them into `~/raw/organized/` by codec family + resolution
3. **Generates** ProRes 422 Proxy for each file in `~/raw/organized/proxies/`
4. **Creates** a Resolve project named `Day-01-Shoot` (or writes a manifest if Resolve isn't running)
5. **Verifies** integrity via checksums, creating a baseline snapshot for future cron runs

### Output (success path)

```
Step 1: probe
  Probed 4 file(s)
Step 2: organize
  Moved 4, skipped 0
Step 3: proxy
  Generated 4 proxy file(s)
Step 4: resolve-project
  Created Resolve project (v20.0)
Step 5: verify
  Clean: 4 file(s) verified

Done.
```

### Output (Resolve unavailable)

If DaVinci Resolve isn't running, step 4 falls back to writing a manifest:

```
Step 4: resolve-project
  Wrote manifest (Resolve not available)
```

You can find the manifest at `~/raw/organized/Day-01-Shoot.drp.manifest.json` and use it to manually create the project in Resolve later.

## Resulting filesystem

After the run, your folder looks like this:

```
~/raw/
├── A001_C001.mov       # original files (unchanged)
├── A001_C002.mov
├── B001_C001.mov
├── sub/
│   └── C001_C001.mov
├── organized/          # NEW: organized by codec + resolution
│   ├── prores/
│   │   └── 1080p/
│   │       ├── A001_C001.mov
│   │       ├── A001_C002.mov
│   │       ├── B001_C001.mov
│   │       └── sub/
│   │           └── C001_C001.mov
│   ├── Day-01-Shoot.drp              # Resolve project (if Resolve was running)
│   └── Day-01-Shoot.drp.manifest.json # Manifest (always written for backup)
└── organized/proxies/                # NEW: proxy files mirror source layout
    ├── prores/
    │   └── 1080p/
    │       ├── A001_C001.mov
    │       ├── A001_C002.mov
    │       ├── B001_C001.mov
    │       └── sub/
    │           └── C001_C001.mov
```

The proxies are in the same codec-family + resolution layout as the originals — easy to find, easy to swap, easy to relink in your NLE.

## Inspecting the audit log

After the run, the audit log at `~/.media-mate/media-mate.db` has rows for everything that happened:

```bash
media-mate log
```

```
┏━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ID ┃ Started              ┃ Status  ┃ Command                                          ┃
┡━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 5  │ 2026-06-26T19:01:32Z │ success │ media-mate verify /Users/me/raw                 │
│ 4  │ 2026-06-26T19:01:31Z │ success │ media-mate resolve create /Users/me/raw/organi…  │
│ 3  │ 2026-06-26T19:00:54Z │ success │ media-mate proxy /Users/me/raw/organized --out…  │
│ 2  │ 2026-06-26T19:00:42Z │ success │ media-mate organize /Users/me/raw --root /User…  │
│ 1  │ 2026-06-26T19:00:31Z │ success │ media-mate probe /Users/me/raw                  │
└────┴─────────────────────┴─────────┴──────────────────────────────────────────────────┘
```

You can query it as JSON for piping into other tools:

```bash
media-mate log --format json | jq '.[0]'
```

```json
{
  "id": 5,
  "started_at": "2026-06-26T19:01:32Z",
  "status": "success",
  "command": "media-mate verify /Users/me/raw"
}
```

## Re-running

The next time you add files to `~/raw/` and run:

```bash
media-mate run ~/raw/ --organize --proxy --resolve-project --verify
```

The verify step will tell you what's changed since the previous run. If you only added new files, exit code 3; if you modified existing files, exit code 2; if you deleted files, exit code 1. Wrap it in cron and you have a nightly integrity check.

## What happens if something fails mid-pipeline

Each step writes its own audit-log row independently. If step 3 (proxy) fails halfway through, steps 1 and 2 are already committed in the audit log. You can re-run the command and steps 1-2 will be fast (probe just re-reads existing files; organize's idempotent on already-organized files).

```bash
media-mate log  # see which step failed
# Fix whatever broke (e.g., install ffmpeg)
media-mate run ~/raw/ --organize --proxy --resolve-project --verify  # retry
```