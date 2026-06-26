"""Tests for the organize capability in organize.py."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from media_mate.log import LogStore
from media_mate.models import (
    MediaMateConfig,
    OrganizeConfig,
    ProbeRecord,
)
from media_mate.organize import (
    OrganizeError,
    _unique_path,
    build_destination_path,
    codec_family,
    organize_path,
    resolution_bucket,
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestCodecFamily:
    @pytest.mark.parametrize(
        "codec,expected",
        [
            ("h264", "h264"),
            ("avc", "h264"),
            ("avc1", "h264"),
            ("H264", "h264"),  # case-insensitive
            ("h265", "h265"),
            ("hevc", "h265"),
            ("prores", "prores"),
            ("vp9", "modern"),
            ("av1", "modern"),
            ("dnxhd", "dnx"),
            ("dnxhr", "dnx"),
            ("mp3", "audio"),
            ("aac", "audio"),
            ("flac", "audio"),
            ("pcm_s24le", "audio"),
        ],
    )
    def test_known_codecs(self, codec: str, expected: str) -> None:
        assert codec_family(codec) == expected

    def test_none_returns_unknown(self) -> None:
        assert codec_family(None) == "unknown"

    def test_unknown_codec_returns_lowercase_name(self) -> None:
        # Unknown codecs preserve their name (in lowercase) for debuggability
        assert codec_family("SomeNewCodec") == "somenewcodec"

    def test_empty_string_returns_empty(self) -> None:
        assert codec_family("") == ""


class TestResolutionBucket:
    @pytest.mark.parametrize(
        "height,expected",
        [
            (None, "unknown"),
            (0, "unknown"),
            (-1, "unknown"),
            (240, "480p"),
            (480, "480p"),
            (720, "720p"),
            (1080, "1080p"),
            (1440, "1440p"),
            (2160, "4K"),
            (4320, "8K"),
            (5000, "5000p"),  # beyond 8K
        ],
    )
    def test_buckets(self, height: int | None, expected: str) -> None:
        assert resolution_bucket(height) == expected


# ---------------------------------------------------------------------------
# Path building
# ---------------------------------------------------------------------------


class TestBuildDestinationPath:
    def test_default_template(self, tmp_path: Path) -> None:
        source = Path("/in/clip.mov")
        dest = build_destination_path(
            "{root}/{codec_family}/{resolution_bucket}/{filename}{ext}",
            tmp_path,
            source,
            "prores",
            "1080p",
        )
        assert dest == tmp_path / "prores" / "1080p" / "clip.mov"

    def test_custom_template_with_date(self, tmp_path: Path) -> None:
        source = Path("/in/clip.mov")
        dest = build_destination_path(
            "{root}/{date}/{codec_family}/{filename}{ext}",
            tmp_path,
            source,
            "h264",
            "1080p",
            date="2026-06-26",
        )
        assert dest == tmp_path / "2026-06-26" / "h264" / "clip.mov"

    def test_flat_template(self, tmp_path: Path) -> None:
        source = Path("/in/clip.mov")
        dest = build_destination_path(
            "{root}/{filename}{ext}",
            tmp_path,
            source,
            "h264",
            "1080p",
        )
        assert dest == tmp_path / "clip.mov"

    def test_date_defaults_to_today(self, tmp_path: Path) -> None:
        source = Path("/in/clip.mov")
        dest = build_destination_path(
            "{root}/{date}/{filename}{ext}",
            tmp_path,
            source,
            "h264",
            "1080p",
        )
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert dest == tmp_path / today / "clip.mov"


class TestUniquePath:
    def test_no_collision_returns_input(self, tmp_path: Path) -> None:
        dest = tmp_path / "clip.mov"
        assert _unique_path(dest) == dest

    def test_collision_appends_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "clip.mov").write_bytes(b"a")
        result = _unique_path(tmp_path / "clip.mov")
        assert result == tmp_path / "clip-1.mov"

    def test_multiple_collisions(self, tmp_path: Path) -> None:
        (tmp_path / "clip.mov").write_bytes(b"a")
        (tmp_path / "clip-1.mov").write_bytes(b"b")
        (tmp_path / "clip-2.mov").write_bytes(b"c")
        result = _unique_path(tmp_path / "clip.mov")
        assert result == tmp_path / "clip-3.mov"


# ---------------------------------------------------------------------------
# organize_path tests
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path_factory) -> Path:
    """A separate directory for the audit log, OUTSIDE any probed tree."""
    return tmp_path_factory.mktemp("media_mate_store")


def _make_store(store_dir: Path) -> LogStore:
    store_dir.mkdir(parents=True, exist_ok=True)
    s = LogStore(store_dir / "log.db")
    s.initialize()
    return s


def _seed_probe(
    store: LogStore,
    path: str,
    *,
    codec: str = "h264",
    height: int = 1080,
    size: int = 1024,
) -> None:
    """Insert a file + probe row for `path` so organize can find it."""
    run_id = store.start_run("seed")
    file_id = store.upsert_file(path, size=size, mtime=0.0, run_id=run_id)
    store.insert_probe(
        ProbeRecord(
            file_id=file_id,
            run_id=run_id,
            codec=codec,
            container="mov",
            width=1920 if (height or 0) >= 1080 else 1280,
            height=height,
            frame_rate=24.0,
            color_space="bt709",
            bit_depth=8,
            duration=60.0,
            audio_channels=2,
            audio_sample_rate=48000,
            probed_at=datetime.now(UTC),
        )
    )


def _count_rows(store: LogStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class TestOrganizePath:
    def test_source_does_not_exist(self, tmp_path: Path, store_dir: Path) -> None:
        store = _make_store(store_dir)
        with pytest.raises(OrganizeError):
            organize_path(tmp_path / "nope", tmp_path / "out", store)

    def test_source_is_not_directory(self, tmp_path: Path, store_dir: Path) -> None:
        f = tmp_path / "clip.mov"
        f.write_bytes(b"x")
        store = _make_store(store_dir)
        with pytest.raises(OrganizeError):
            organize_path(f, tmp_path / "out", store)

    def test_empty_source(self, tmp_path: Path, store_dir: Path) -> None:
        empty = tmp_path / "in"
        empty.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        result = organize_path(empty, out, store)

        assert result.files_moved == 0
        assert result.files_skipped == 0
        assert result.bytes_moved == 0
        assert result.errors == []
        # No run was created for an empty directory
        assert _count_rows(store, "runs") == 0

    def test_moves_probed_files(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        # Two probed files
        for name in ("a.mov", "b.mov"):
            p = src / name
            p.write_bytes(b"x" * 1024)
            _seed_probe(store, str(p), codec="h264", height=1080, size=1024)

        result = organize_path(src, out, store)

        assert result.files_moved == 2
        assert result.files_skipped == 0
        assert result.bytes_moved == 2048
        assert not (src / "a.mov").exists()
        assert (out / "h264" / "1080p" / "a.mov").exists()
        assert (out / "h264" / "1080p" / "b.mov").exists()

        # Audit log got the rows
        assert _count_rows(store, "organize_ops") == 2

    def test_skips_unprobed_files(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "probed.mov").write_bytes(b"x")
        _seed_probe(store, str(src / "probed.mov"), codec="h264", height=1080)

        (src / "unprobed.mov").write_bytes(b"x")
        # No probe seeded for this one

        result = organize_path(src, out, store)

        assert result.files_moved == 1
        assert result.files_skipped == 1
        assert any("unprobed.mov" in e and "no probe data" in e for e in result.errors)
        assert (out / "h264" / "1080p" / "probed.mov").exists()
        # Unprobed file remains in source
        assert (src / "unprobed.mov").exists()

    def test_dry_run_does_not_move(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "a.mov").write_bytes(b"x" * 100)
        _seed_probe(store, str(src / "a.mov"))

        result = organize_path(src, out, store, dry_run=True)

        assert result.files_moved == 1
        assert result.dry_run is True
        # File still in source
        assert (src / "a.mov").exists()
        # Destination not created
        assert not (out / "h264" / "1080p" / "a.mov").exists()
        # No organize_ops rows for dry-run
        assert _count_rows(store, "organize_ops") == 0

    def test_conflict_skip(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        # Pre-create destination
        dest_dir = out / "h264" / "1080p"
        dest_dir.mkdir(parents=True)
        (dest_dir / "a.mov").write_bytes(b"existing")

        (src / "a.mov").write_bytes(b"new")
        _seed_probe(store, str(src / "a.mov"))

        result = organize_path(src, out, store)

        assert result.files_moved == 0
        assert result.files_skipped == 1
        assert any("destination already exists" in e for e in result.errors)
        # Source still has the file
        assert (src / "a.mov").exists()
        # Destination untouched (still has 'existing' content)
        assert (dest_dir / "a.mov").read_bytes() == b"existing"

    def test_conflict_rename(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        dest_dir = out / "h264" / "1080p"
        dest_dir.mkdir(parents=True)
        (dest_dir / "a.mov").write_bytes(b"existing")

        (src / "a.mov").write_bytes(b"new")
        _seed_probe(store, str(src / "a.mov"))

        cfg = MediaMateConfig(organize=OrganizeConfig(on_conflict="rename"))
        result = organize_path(src, out, store, config=cfg)

        assert result.files_moved == 1
        assert (dest_dir / "a.mov").read_bytes() == b"existing"
        assert (dest_dir / "a-1.mov").read_bytes() == b"new"

    def test_conflict_overwrite(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        dest_dir = out / "h264" / "1080p"
        dest_dir.mkdir(parents=True)
        (dest_dir / "a.mov").write_bytes(b"existing")

        (src / "a.mov").write_bytes(b"new")
        _seed_probe(store, str(src / "a.mov"))

        cfg = MediaMateConfig(organize=OrganizeConfig(on_conflict="overwrite"))
        result = organize_path(src, out, store, config=cfg)

        assert result.files_moved == 1
        assert (dest_dir / "a.mov").read_bytes() == b"new"

    def test_mixed_codec_families(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "video.mp4").write_bytes(b"v")
        _seed_probe(store, str(src / "video.mp4"), codec="h264", height=1080)

        (src / "audio.mp3").write_bytes(b"a")
        _seed_probe(store, str(src / "audio.mp3"), codec="mp3", height=None)

        result = organize_path(src, out, store)

        assert result.files_moved == 2
        assert (out / "h264" / "1080p" / "video.mp4").exists()
        assert (out / "audio" / "unknown" / "audio.mp3").exists()

    def test_uses_default_template(self, tmp_path: Path, store_dir: Path) -> None:
        """Sanity check that the default OrganizeConfig template is applied."""
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "clip.mov").write_bytes(b"x")
        _seed_probe(store, str(src / "clip.mov"), codec="prores", height=1080)

        organize_path(src, out, store)
        assert (out / "prores" / "1080p" / "clip.mov").exists()

    def test_custom_template(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "clip.mov").write_bytes(b"x")
        _seed_probe(store, str(src / "clip.mov"))

        cfg = MediaMateConfig(organize=OrganizeConfig(template="{root}/{filename}{ext}"))
        organize_path(src, out, store, config=cfg)

        assert (out / "clip.mov").exists()

    def test_run_status_partial(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "probed.mov").write_bytes(b"x")
        _seed_probe(store, str(src / "probed.mov"))

        (src / "unprobed.mov").write_bytes(b"x")

        organize_path(src, out, store)

        with sqlite3.connect(store.db_path) as conn:
            status = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert status == "partial"

    def test_run_status_success(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "a.mov").write_bytes(b"x")
        _seed_probe(store, str(src / "a.mov"))

        organize_path(src, out, store)

        with sqlite3.connect(store.db_path) as conn:
            status = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert status == "success"

    def test_recursive(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        sub = src / "sub"
        sub.mkdir(parents=True)
        out = tmp_path / "out"
        store = _make_store(store_dir)

        (src / "top.mov").write_bytes(b"x")
        (sub / "deep.mov").write_bytes(b"y")
        _seed_probe(store, str(src / "top.mov"))
        _seed_probe(store, str(sub / "deep.mov"))

        result = organize_path(src, out, store)

        assert result.files_moved == 2
        assert (out / "h264" / "1080p" / "top.mov").exists()
        assert (out / "h264" / "1080p" / "deep.mov").exists()
