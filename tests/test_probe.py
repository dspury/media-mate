"""Tests for the probe capability in probe.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_mate.log import LogStore
from media_mate.probe import (
    ProbeError,
    _parse_ffprobe_output,
    _parse_frame_rate,
    _safe_float,
    _safe_int,
    find_ffprobe,
    probe_file,
    probe_path,
)

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


class TestParseFrameRate:
    def test_simple_fraction(self) -> None:
        assert _parse_frame_rate("24/1") == 24.0

    def test_ntsc_fraction(self) -> None:
        # 30000/1001 ≈ 29.97003
        assert _parse_frame_rate("30000/1001") == pytest.approx(29.97003, rel=1e-4)

    def test_zero_over_zero(self) -> None:
        assert _parse_frame_rate("0/0") is None

    def test_zero_denominator(self) -> None:
        assert _parse_frame_rate("24/0") is None

    def test_empty(self) -> None:
        assert _parse_frame_rate("") is None

    def test_none(self) -> None:
        assert _parse_frame_rate(None) is None

    def test_plain_number(self) -> None:
        assert _parse_frame_rate("25") == 25.0

    def test_invalid(self) -> None:
        assert _parse_frame_rate("not a number") is None


class TestSafeFloat:
    def test_valid_string(self) -> None:
        assert _safe_float("1.5") == 1.5

    def test_valid_number(self) -> None:
        assert _safe_float(1.5) == 1.5

    def test_none(self) -> None:
        assert _safe_float(None) is None

    def test_empty(self) -> None:
        assert _safe_float("") is None

    def test_na(self) -> None:
        assert _safe_float("N/A") is None

    def test_invalid(self) -> None:
        assert _safe_float("xyz") is None


class TestSafeInt:
    def test_valid_string(self) -> None:
        assert _safe_int("42") == 42

    def test_valid_number(self) -> None:
        assert _safe_int(42) == 42

    def test_none(self) -> None:
        assert _safe_int(None) is None

    def test_empty(self) -> None:
        assert _safe_int("") is None

    def test_na(self) -> None:
        assert _safe_int("N/A") is None

    def test_invalid(self) -> None:
        assert _safe_int("xyz") is None


# ---------------------------------------------------------------------------
# Sample ffprobe JSON fixtures
# ---------------------------------------------------------------------------


SAMPLE_FFPROBE_VIDEO_AUDIO = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "24/1",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "bits_per_raw_sample": "8",
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "sample_rate": "48000",
            "bits_per_sample": "16",
        },
    ],
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "120.500000",
        "size": "104857600",
    },
}


SAMPLE_FFPROBE_AUDIO_ONLY = {
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "mp3",
            "channels": 1,
            "sample_rate": "44100",
        },
    ],
    "format": {
        "format_name": "mp3",
        "duration": "180.000000",
        "size": "2880000",
    },
}


SAMPLE_FFPROBE_NTSC = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "color_space": "bt709",
        },
    ],
    "format": {
        "format_name": "mov",
        "duration": "60.000000",
    },
}


# ---------------------------------------------------------------------------
# _parse_ffprobe_output tests
# ---------------------------------------------------------------------------


class TestParseFfprobeOutput:
    def test_video_with_audio(self, tmp_path: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")
        probe = _parse_ffprobe_output(p, SAMPLE_FFPROBE_VIDEO_AUDIO)
        assert probe.path == str(p)
        assert probe.video_codec == "h264"
        assert probe.width == 1920
        assert probe.height == 1080
        assert probe.frame_rate == 24.0
        assert probe.color_space == "bt709"
        assert probe.color_transfer == "bt709"
        assert probe.color_primaries == "bt709"
        assert probe.bit_depth == 8
        assert probe.audio_codec == "aac"
        assert probe.audio_channels == 2
        assert probe.audio_sample_rate == 48000
        assert probe.audio_bit_depth == 16
        assert probe.duration_seconds == 120.5
        assert probe.file_size_bytes == 104857600
        assert probe.container == "mov,mp4,m4a,3gp,3g2,mj2"

    def test_audio_only(self, tmp_path: Path) -> None:
        p = tmp_path / "song.mp3"
        p.write_bytes(b"fake")
        probe = _parse_ffprobe_output(p, SAMPLE_FFPROBE_AUDIO_ONLY)
        assert probe.video_codec is None
        assert probe.width is None
        assert probe.height is None
        assert probe.audio_codec == "mp3"
        assert probe.audio_channels == 1
        assert probe.audio_sample_rate == 44100
        assert probe.duration_seconds == 180.0

    def test_ntsc_frame_rate(self, tmp_path: Path) -> None:
        p = tmp_path / "ntsc.mov"
        p.write_bytes(b"fake")
        probe = _parse_ffprobe_output(p, SAMPLE_FFPROBE_NTSC)
        assert probe.frame_rate == pytest.approx(29.97003, rel=1e-4)

    def test_missing_format_size_falls_back_to_stat(self, tmp_path: Path) -> None:
        p = tmp_path / "no_size.mov"
        p.write_bytes(b"x" * 100)
        raw = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 480,
                    "avg_frame_rate": "30/1",
                }
            ],
            "format": {"format_name": "mov", "duration": "10.0"},
        }
        probe = _parse_ffprobe_output(p, raw)
        assert probe.file_size_bytes == 100
        assert probe.width == 640
        assert probe.height == 480


# ---------------------------------------------------------------------------
# find_ffprobe tests
# ---------------------------------------------------------------------------


class TestFindFfprobe:
    def test_finds_on_path(self) -> None:
        with patch("media_mate.probe.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/ffprobe"
            result = find_ffprobe()
            assert result == "/usr/bin/ffprobe"

    def test_falls_back_to_ffmpeg_dir(self) -> None:
        with patch("media_mate.probe.shutil.which") as mock_which:
            # First call (ffprobe from config dir) returns None, second (which("ffprobe")) returns None too,
            # then we'd check the config-derived path which is a real file.
            mock_which.side_effect = [None, None]

            from media_mate.models import MediaMateConfig

            with patch("pathlib.Path.is_file", return_value=True):
                config = MediaMateConfig(ffmpeg_path="/usr/local/bin/ffmpeg")
                # The candidate derived from config is "/usr/local/bin/ffprobe"
                # With is_file mocked to True, find_ffprobe should return it
                result = find_ffprobe(config)
                assert "ffprobe" in result

    def test_raises_when_not_found(self) -> None:
        with (
            patch("media_mate.probe.shutil.which") as mock_which,
            patch("pathlib.Path.is_file", return_value=False),
            pytest.raises(ProbeError),
        ):
            mock_which.return_value = None
            find_ffprobe()


# ---------------------------------------------------------------------------
# probe_file tests (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestProbeFile:
    def test_returns_probe(self, tmp_path: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            probe = probe_file(p)
            assert probe.video_codec == "h264"
            assert probe.width == 1920

    def test_uses_provided_ffprobe_path(self, tmp_path: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            probe_file(p, ffprobe_path="/custom/path/ffprobe")
            # Check the cmd used the custom path
            args = mock_run.call_args[0][0]
            assert args[0] == "/custom/path/ffprobe"

    def test_file_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.mov"
        with pytest.raises(ProbeError):
            probe_file(p)

    def test_ffprobe_error_exit_code(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.mov"
        p.write_bytes(b"garbage")

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Invalid data found when processing input",
            )
            with pytest.raises(ProbeError) as exc_info:
                probe_file(p)
            assert "Invalid data" in exc_info.value.reason

    def test_ffprobe_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="this is not json",
                stderr="",
            )
            with pytest.raises(ProbeError) as exc_info:
                probe_file(p)
            assert "invalid JSON" in exc_info.value.reason

    def test_ffprobe_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ffprobe missing")
            with pytest.raises(ProbeError):
                probe_file(p)


# ---------------------------------------------------------------------------
# probe_path tests
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path_factory) -> Path:
    """A directory OUTSIDE any probed tree, for the audit log.

    Must not be a child of tmp_path or any directory passed to probe_path,
    otherwise probe_path's rglob will pick up log.db and try to ffprobe it.
    """
    return tmp_path_factory.mktemp("media_mate_store")


def _make_store(store_dir: Path) -> LogStore:
    """Create a LogStore in the given directory."""
    store_dir.mkdir(parents=True, exist_ok=True)
    s = LogStore(store_dir / "log.db")
    s.initialize()
    return s


def _count_rows(store: LogStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _latest_run_status(store: LogStore) -> tuple[str, str | None]:
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT status, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], row[1])


class TestProbePath:
    def test_probes_single_file(self, tmp_path: Path, store_dir: Path) -> None:
        p = tmp_path / "clip.mov"
        p.write_bytes(b"fake")
        store = _make_store(store_dir)

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            results = probe_path(p, store)

        assert len(results) == 1
        assert results[0].video_codec == "h264"
        assert _count_rows(store, "files") == 1
        assert _count_rows(store, "probes") == 1
        assert _latest_run_status(store)[0] == "success"

    def test_probes_directory_recursively(self, tmp_path: Path, store_dir: Path) -> None:
        (tmp_path / "a.mov").write_bytes(b"a")
        (tmp_path / "b.mp4").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.mxf").write_bytes(b"c")
        store = _make_store(store_dir)

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            results = probe_path(tmp_path, store)

        assert len(results) == 3
        assert _count_rows(store, "files") == 3
        assert _count_rows(store, "probes") == 3
        assert _latest_run_status(store)[0] == "success"

    def test_empty_directory(self, tmp_path: Path, store_dir: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        store = _make_store(store_dir)

        results = probe_path(empty, store)

        assert results == []
        # No runs should be created for an empty directory
        assert _count_rows(store, "runs") == 0

    def test_partial_failure_marks_run_partial(self, tmp_path: Path, store_dir: Path) -> None:
        (tmp_path / "good.mov").write_bytes(b"good")
        (tmp_path / "bad.mov").write_bytes(b"bad")
        store = _make_store(store_dir)

        call_count = {"n": 0}

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                return MagicMock(
                    returncode=0,
                    stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                    stderr="",
                )
            return MagicMock(returncode=1, stdout="", stderr="Invalid data")

        with patch("media_mate.probe.subprocess.run", side_effect=fake_run):
            results = probe_path(tmp_path, store)

        # Exactly one of the two files succeeded
        assert len(results) == 1
        status, error = _latest_run_status(store)
        assert status == "partial"
        assert error is not None
        assert "1 file(s) failed" in error

    def test_all_failure_marks_run_failed(self, tmp_path: Path, store_dir: Path) -> None:
        (tmp_path / "bad1.mov").write_bytes(b"bad1")
        (tmp_path / "bad2.mov").write_bytes(b"bad2")
        store = _make_store(store_dir)

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Invalid data")
            results = probe_path(tmp_path, store)

        assert results == []
        status, error = _latest_run_status(store)
        assert status == "failed"
        assert error is not None
        assert "2 file(s) failed" in error

    def test_path_does_not_exist(self, tmp_path: Path, store_dir: Path) -> None:
        store = _make_store(store_dir)
        with pytest.raises(ProbeError):
            probe_path(tmp_path / "nope", store)

    def test_skips_system_artifacts_on_camera_cards(self, tmp_path: Path, store_dir: Path) -> None:
        """AppleDouble sidecars, .DS_Store and recycle bins are never probed.

        macOS writes ._clip.MP4 sidecars on exFAT camera cards; ffprobe fails
        on every one of them, which used to poison probe runs from external
        drives (reported from the TUI as 'did not probe properly').
        """
        (tmp_path / "clip.MP4").write_bytes(b"real")
        (tmp_path / "._clip.MP4").write_bytes(b"appledouble")
        (tmp_path / ".DS_Store").write_bytes(b"junk")
        recycle = tmp_path / "$RECYCLE.BIN"
        recycle.mkdir()
        (recycle / "old.mp4").write_bytes(b"deleted")
        trashes = tmp_path / ".Trashes" / "501"
        trashes.mkdir(parents=True)
        (trashes / "gone.mov").write_bytes(b"deleted")
        store = _make_store(store_dir)

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            results = probe_path(tmp_path, store)

        assert [Path(r.path).name for r in results] == ["clip.MP4"]
        assert _latest_run_status(store)[0] == "success"

    def test_scan_rooted_inside_hidden_directory_still_works(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        """Only components BELOW the scan root count as junk."""
        hidden_root = tmp_path / ".staging"
        hidden_root.mkdir()
        (hidden_root / "clip.mov").write_bytes(b"real")
        store = _make_store(store_dir)

        with patch("media_mate.probe.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO),
                stderr="",
            )
            results = probe_path(hidden_root, store)

        assert len(results) == 1

    def test_on_file_callback_reports_successes_and_failures(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        (tmp_path / "good.mov").write_bytes(b"good")
        (tmp_path / "worse.mov").write_bytes(b"worse")
        store = _make_store(store_dir)

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            target = args[0][-1]
            if "good" in target:
                return MagicMock(
                    returncode=0, stdout=json.dumps(SAMPLE_FFPROBE_VIDEO_AUDIO), stderr=""
                )
            return MagicMock(returncode=1, stdout="", stderr="Invalid data")

        seen: list[tuple[str, str | None]] = []
        with patch("media_mate.probe.subprocess.run", side_effect=fake_run):
            probe_path(tmp_path, store, on_file=lambda f, err: seen.append((f.name, err)))

        assert ("good.mov", None) in seen
        failures = [(name, err) for name, err in seen if err is not None]
        assert len(failures) == 1
        assert failures[0][0] == "worse.mov"
        assert "Invalid data" in (failures[0][1] or "")


class TestIsSystemArtifact:
    def test_dot_prefixed_components(self, tmp_path: Path) -> None:
        from media_mate.probe import is_system_artifact

        assert is_system_artifact(tmp_path / "._clip.MP4", tmp_path)
        assert is_system_artifact(tmp_path / ".DS_Store", tmp_path)
        assert is_system_artifact(tmp_path / ".Trashes" / "501" / "x.mov", tmp_path)

    def test_named_artifacts(self, tmp_path: Path) -> None:
        from media_mate.probe import is_system_artifact

        assert is_system_artifact(tmp_path / "$RECYCLE.BIN" / "x.mp4", tmp_path)
        assert is_system_artifact(tmp_path / "System Volume Information" / "y", tmp_path)
        assert is_system_artifact(tmp_path / "Thumbs.db", tmp_path)

    def test_real_media_is_not_junk(self, tmp_path: Path) -> None:
        from media_mate.probe import is_system_artifact

        assert not is_system_artifact(tmp_path / "CANON R5 — Oct 17" / "C9927.MP4", tmp_path)

    def test_hidden_scan_root_does_not_poison_children(self, tmp_path: Path) -> None:
        from media_mate.probe import is_system_artifact

        root = tmp_path / ".staging"
        assert not is_system_artifact(root / "clip.mov", root)
