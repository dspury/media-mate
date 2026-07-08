# media-mate Walkthrough

A visual end-to-end walkthrough of media-mate's full pipeline, using the test dataset in this folder.

## Test dataset

The `test-dataset/raw/` folder contains five synthetic media files of varying codecs and resolutions:

```
test-dataset/raw/
├── A001_C001.mov   # H.264, 1920x1080, 2s
├── A001_C002.mov   # H.264, 1280x720,  2s
├── B001_C001.mov   # ProRes 422, 1920x1080, 2s
├── B001_C002.mov   # H.264, 3840x2160 (4K), 2s
└── sub/
    └── C001_C001.mov  # H.264, 1920x1080, 2s
```

These are generated with FFmpeg's `testsrc` pattern + sine wave audio — no real media required.

## Run the full pipeline

```bash
cd examples/
./run-demo.sh
```

## What it does

### Probe

Extracts codec, resolution, frame rate, color space, audio channels, duration, and file size from every file.

```
$ media-mate probe test-dataset/raw/

  Probed 5 file(s)
  ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┓
  ┃ File                            ┃ Codec     ┃ Resolution   ┃ Duration ┃
  ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━┩
  │ raw/A001_C001.mov               │ h264      │ 1920x1080   │ 2.0s    │
  │ raw/A001_C002.mov               │ h264      │ 1280x720    │ 2.0s    │
  │ raw/B001_C001.mov               │ prores    │ 1920x1080   │ 2.0s    │
  │ raw/B001_C002.mov               │ h264      │ 3840x2160   │ 2.0s    │
  │ raw/sub/C001_C001.mov           │ h264      │ 1920x1080   │ 2.0s    │
  └─────────────────────────────────┴───────────┴──────────────┴──────────┘
```

### Organize

Moves files into a structured layout: `<codec_family>/<resolution_bucket>/<filename>`. Subdirectory structure is preserved.

```
$ media-mate organize test-dataset/raw/ --root test-dataset/output/organized/

  Copied 5 file(s), skipped 0, 2,847,362 bytes total
```

Resulting layout:
```
output/organized/
├── h264/
│   ├── 1080p/
│   │   ├── A001_C001.mov
│   │   └── sub/
│   │       └── C001_C001.mov
│   └── 720p/
│       └── A001_C002.mov
├── h264_4k/
│   └── 2160p/
│       └── B001_C002.mov
└── prores/
    └── 1080p/
        └── B001_C001.mov
```

### Proxy

Generates ProRes 422 Proxy files at 1080p for every source file. Aspect ratio is preserved (letter-boxed if needed). Skips files that already have a proxy.

```
$ media-mate proxy test-dataset/output/organized/ --out test-dataset/output/proxies/

  Generated 5 proxy file(s)
```

Proxies mirror the source layout:
```
output/proxies/
├── h264/1080p/A001_C001.mov     # ProRes 422 Proxy
├── h264/720p/A001_C002.mov      # ProRes 422 Proxy
├── h264_4k/2160p/B001_C002.mov  # ProRes 422 Proxy
├── prores/1080p/B001_C001.mov   # (already proxy-quality, still copied)
└── sub/C001_C001.mov            # ProRes 422 Proxy
```

### DaVinci Resolve project

Creates a Resolve project programmatically. When Resolve is available (and running), the project is created live. When it's not available, a JSON manifest is written so you can recreate the project manually.

```
$ media-mate resolve create test-dataset/output/organized/ \
    --project "Demo-Project" \
    --resolution 1080 \
    --fps 24 \
    --proxy-dir test-dataset/output/proxies/ \
    --output test-dataset/output/Demo-Project.drp

  Resolve not available; wrote manifest file at
  test-dataset/output/organized/Demo-Project.drp.manifest.json
```

### Verify

Computes checksums for every file. First run creates a baseline; subsequent runs compare against it and report what changed.

```
$ media-mate verify test-dataset/output/organized/

  Clean: 5 file(s) verified
```

If files are added, modified, or deleted between runs, `verify` reports them explicitly with structured exit codes (0=clean, 1=missing, 2=modified, 3=added) — designed for cron + alerting.

## Resulting filesystem

```
test-dataset/output/
├── organized/
│   ├── h264/
│   │   ├── 1080p/A001_C001.mov
│   │   ├── 720p/A001_C002.mov
│   │   └── sub/C001_C001.mov
│   ├── h264_4k/2160p/B001_C002.mov
│   ├── prores/1080p/B001_C001.mov
│   └── Demo-Project.drp.manifest.json
├── proxies/
│   ├── h264/1080p/A001_C001.mov
│   ├── h264/720p/A001_C002.mov
│   ├── h264_4k/2160p/B001_C002.mov
│   ├── prores/1080p/B001_C001.mov
│   └── sub/C001_C001.mov
└── Demo-Project.drp.manifest.json
```

## Audit log

Every step writes to the SQLite audit log at `~/.media-mate/media-mate.db`. Query it any time:

```
$ media-mate log

  ┏━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
  ┃ ID ┃ Started              ┃ Status  ┃ Command                                   ┃
  ┡━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
  │ 6  │ 2026-07-05T00:00:00Z│ success │ media-mate verify .../organized          │
  │ 5  │ 2026-07-05T00:00:01Z│ success │ media-mate resolve create .../organized   │
  │ 4  │ 2026-07-05T00:00:00Z│ success │ media-mate proxy .../organized --out ...  │
  │ 3  │ 2026-07-05T00:00:00Z│ success │ media-mate organize ...raw --root ...     │
  │ 2  │ 2026-07-05T00:00:00Z│ success │ media-mate probe .../raw                  │
  └────┴─────────────────────┴─────────┴──────────────────────────────────────────┘
```

The log is the system of record — back it up, copy it between machines, query it with `--format json` to pipe into other tools.

## Cleanup

```bash
rm -rf test-dataset/output/
media-mate log  # still shows history
```
