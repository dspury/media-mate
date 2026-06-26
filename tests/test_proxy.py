"""Tests for the proxy capability in proxy.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_mate.log import LogStore
from media_mate.models import MediaMateConfig, ProxyRequest
from media_mate.proxy import (
    ProxyError,
    _format_errors,
    _profile_for,
    find_ffmpeg,
    generate_proxies,
    generate_proxy,
)

# ---------------------------------------------------------------------------
# find_ffmpeg tests
# ---------------------------------------------------------------------------


class TestFindFfmpeg:
    def test_finds_on_path(self) -> None:
        with patch("media_mate.proxy.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/ffmpeg"
            assert find_ffmpeg() == "/usr/bin/ffmpeg"

    def test_uses_config_path_when_file_exists(self) -> None:
        with patch("pathlib.Path.is_file", return_value=True):
            cfg = MediaMateConfig(ffmpeg_path="/custom/ffmpeg")
            result = find_ffmpeg(cfg)
            assert result == "/custom/ffmpeg"

    def test_falls_back_to_path_when_config_invalid(self) -> None:
        with (
            patch("pathlib.Path.is_file", return_value=False),
            patch("media_mate.proxy.shutil.which") as mock_which,
        ):
            mock_which.return_value = "/usr/bin/ffmpeg"
            cfg = MediaMateConfig(ffmpeg_path="/nonexistent/ffmpeg")
            result = find_ffmpeg(cfg)
            assert result == "/usr/bin/ffmpeg"

    def test_raises_when_not_found(self) -> None:
        with (
            patch("pathlib.Path.is_file", return_value=False),
            patch("media_mate.proxy.shutil.which") as mock_which,
            pytest.raises(ProxyError),
        ):
            mock_which.return_value = None
            find_ffmpeg()


# ---------------------------------------------------------------------------
# _profile_for tests
# ---------------------------------------------------------------------------


class TestProfileFor:
    @pytest.mark.parametrize(
        "codec,expected",
        [
            ("ProRes422Proxy", 0),
            ("prores422proxy", 0),
            ("ProRes422LT", 1),
            ("ProRes422", 2),
            ("ProRes422HQ", 3),
            ("ProRes4444", 4),
            ("ProRes4444XQ", 5),
        ],
    )
    def test_known_codecs(self, codec: str, expected: int) -> None:
        assert _profile_for(codec) == expected

    def test_unknown_codec_raises(self) -> None:
        with pytest.raises(ProxyError):
            _profile_for("h264")


# ---------------------------------------------------------------------------
# _format_errors tests
# ---------------------------------------------------------------------------


class TestFormatErrors:
    def test_empty(self) -> None:
        assert _format_errors([]) == ""

    def test_under_limit(self) -> None:
        errors = [(Path("a.mov"), "fail A"), (Path("b.mov"), "fail B")]
        result = _format_errors(errors)
        assert "2 file(s) failed" in result
        assert "a.mov: fail A" in result

    def test_over_limit_truncates(self) -> None:
        errors = [(Path(f"f{i}.mov"), f"fail {i}") for i in range(10)]
        result = _format_errors(errors, limit=3)
        assert "10 file(s) failed" in result
        assert "7 more" in result  # 10 total - 3 shown = 7


# ---------------------------------------------------------------------------
# generate_proxy tests (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestGenerateProxy:
    def test_success(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"raw")
        out = tmp_path / "proxy.mov"
        out.write_bytes(b"proxy bytes")

        with (
            patch("media_mate.proxy.subprocess.run") as mock_run,
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_probe.return_value = (1920, 1080, 60.0)
            request = ProxyRequest(source_path=str(src), output_path=str(out))
            result = generate_proxy(request, ffmpeg_path="/custom/ffmpeg")

        assert result.source_path == str(src)
        assert result.proxy_path == str(out)
        assert result.codec == "ProRes422Proxy"
        assert result.width == 1920
        assert result.height == 1080
        assert result.duration_seconds == 60.0
        # ffmpeg was invoked
        args = mock_run.call_args[0][0]
        assert args[0] == "/custom/ffmpeg"

    def test_default_codec_is_proxy(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"raw")
        out = tmp_path / "proxy.mov"
        out.write_bytes(b"p")

        with (
            patch("media_mate.proxy.subprocess.run") as mock_run,
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_probe.return_value = (1920, 1080, 60.0)
            request = ProxyRequest(source_path=str(src), output_path=str(out))
            generate_proxy(request, ffmpeg_path="/ffmpeg")
            args = mock_run.call_args[0][0]
            # Args: [ffmpeg, -y, -i, src, -vf, vf_str, -c:v, codec, -profile:v, profile, -c:a, audio, out]
            assert args[7] == "prores_ks"
            assert args[9] == "0"  # ProRes422Proxy is profile 0

    def test_source_not_found(self, tmp_path: Path) -> None:
        request = ProxyRequest(
            source_path=str(tmp_path / "missing.mov"),
            output_path=str(tmp_path / "out.mov"),
        )
        with pytest.raises(ProxyError):
            generate_proxy(request, ffmpeg_path="/ffmpeg")

    def test_ffmpeg_error_exit_code(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out = tmp_path / "out.mov"

        with patch("media_mate.proxy.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="Conversion failed!\nLast line"
            )
            request = ProxyRequest(source_path=str(src), output_path=str(out))
            with pytest.raises(ProxyError) as exc_info:
                generate_proxy(request, ffmpeg_path="/ffmpeg")
            assert "ffmpeg exited 1" in exc_info.value.reason
            assert "Last line" in exc_info.value.reason

    def test_ffmpeg_not_found(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out = tmp_path / "out.mov"

        with patch("media_mate.proxy.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("ffmpeg missing")
            request = ProxyRequest(source_path=str(src), output_path=str(out))
            with pytest.raises(ProxyError):
                generate_proxy(request, ffmpeg_path="/ffmpeg")

    def test_output_dir_creation_fails(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        # Use an output path under a non-creatable parent (root-owned dir)
        # Actually, simpler: use a path under a file (not a dir)
        blocker = tmp_path / "blocker"
        blocker.write_bytes(b"x")
        out = blocker / "subdir" / "out.mov"

        request = ProxyRequest(source_path=str(src), output_path=str(out))
        with pytest.raises(ProxyError) as exc_info:
            generate_proxy(request, ffmpeg_path="/ffmpeg")
        assert "cannot create output directory" in exc_info.value.reason

    def test_unsupported_codec(self, tmp_path: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out = tmp_path / "out.mov"

        request = ProxyRequest(source_path=str(src), output_path=str(out), codec="h264")
        with pytest.raises(ProxyError) as exc_info:
            generate_proxy(request, ffmpeg_path="/ffmpeg")
        assert "unsupported codec" in exc_info.value.reason

    def test_probe_failure_does_not_block(self, tmp_path: Path) -> None:
        """If post-generation probing fails, we still return a ProxyResult."""
        src = tmp_path / "clip.mov"
        src.write_bytes(b"x")
        out = tmp_path / "out.mov"
        out.write_bytes(b"proxy")

        with (
            patch("media_mate.proxy.subprocess.run") as mock_run,
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_probe.side_effect = RuntimeError("probe failed")
            request = ProxyRequest(source_path=str(src), output_path=str(out))
            result = generate_proxy(request, ffmpeg_path="/ffmpeg")

        # Metadata defaults to zeros
        assert result.width == 0
        assert result.height == 0
        assert result.duration_seconds == 0.0
        assert result.proxy_path == str(out)


# ---------------------------------------------------------------------------
# generate_proxies tests
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media_mate_store")


def _make_store(store_dir: Path) -> LogStore:
    store_dir.mkdir(parents=True, exist_ok=True)
    s = LogStore(store_dir / "log.db")
    s.initialize()
    return s


def _count_rows(store: LogStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _fake_successful_ffmpeg(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Mock subprocess.run that 'succeeds' AND writes a dummy output file.

    subprocess args list: [ffmpeg, -y, -i, src, -vf, vf, -c:v, codec, -profile:v, profile, -c:a, audio, out]
    The output path is the last positional arg.
    """
    output_path = Path(args[0][-1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"fake proxy bytes")
    return MagicMock(returncode=0, stdout="", stderr="")


def _latest_run_status(store: LogStore) -> tuple[str, str | None]:
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT status, error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], row[1])


class TestGenerateProxies:
    def test_source_does_not_exist(self, tmp_path: Path, store_dir: Path) -> None:
        store = _make_store(store_dir)
        with pytest.raises(ProxyError):
            generate_proxies(tmp_path / "nope", tmp_path / "out", store)

    def test_empty_source(self, tmp_path: Path, store_dir: Path) -> None:
        empty = tmp_path / "in"
        empty.mkdir()
        out = tmp_path / "out"
        store = _make_store(store_dir)

        results = generate_proxies(empty, out, store)

        assert results == []
        # No run created for empty source
        assert _count_rows(store, "runs") == 0

    def test_single_file(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "clip.mov"
        src.write_bytes(b"raw")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        with (
            patch(
                "media_mate.proxy.subprocess.run",
                side_effect=_fake_successful_ffmpeg,
            ),
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_probe.return_value = (1920, 1080, 60.0)
            results = generate_proxies(src, out, store)

        assert len(results) == 1
        assert (out / "clip.mov").exists()
        assert _count_rows(store, "proxies") == 1
        assert _latest_run_status(store)[0] == "success"

    def test_recursive_directory(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "a.mov").write_bytes(b"a")
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.mov").write_bytes(b"b")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        with (
            patch(
                "media_mate.proxy.subprocess.run",
                side_effect=_fake_successful_ffmpeg,
            ),
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_probe.return_value = (1920, 1080, 60.0)
            results = generate_proxies(src, out, store)

        assert len(results) == 2
        # Relative subpath preserved
        assert (out / "a.mov").exists()
        assert (out / "sub" / "b.mov").exists()
        assert _count_rows(store, "proxies") == 2

    def test_skip_existing_proxy(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "clip.mov").write_bytes(b"raw")
        out = tmp_path / "out"
        out.mkdir()
        # Pre-create the output
        (out / "clip.mov").write_bytes(b"existing proxy")

        store = _make_store(store_dir)

        results = generate_proxies(src, out, store)

        assert results == []
        status, error = _latest_run_status(store)
        assert status == "failed"
        assert error is not None
        assert "already exists" in error

    def test_partial_failure(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "good.mov").write_bytes(b"good")
        (src / "bad.mov").write_bytes(b"bad")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        call_count = {"n": 0}

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            if call_count["n"] % 2 == 1:
                return _fake_successful_ffmpeg(*args, **kwargs)
            return MagicMock(returncode=1, stdout="", stderr="ffmpeg failed")

        with (
            patch("media_mate.proxy.subprocess.run", side_effect=fake_run),
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_probe.return_value = (1920, 1080, 60.0)
            results = generate_proxies(src, out, store)

        assert len(results) == 1
        status, error = _latest_run_status(store)
        assert status == "partial"
        assert error is not None
        assert "1 file(s) failed" in error

    def test_all_failure(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "bad1.mov").write_bytes(b"b1")
        (src / "bad2.mov").write_bytes(b"b2")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        with patch("media_mate.proxy.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ffmpeg failed")
            results = generate_proxies(src, out, store)

        assert results == []
        status, error = _latest_run_status(store)
        assert status == "failed"
        assert error is not None
        assert "2 file(s) failed" in error

    def test_uses_config_codec_and_height(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "clip.mov").write_bytes(b"x")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        cfg = MediaMateConfig(proxy_codec="ProRes422HQ", proxy_height=720)
        captured_args: list[list[str]] = []

        def capturing_ffmpeg(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured_args.append(list(args[0]))
            return _fake_successful_ffmpeg(*args, **kwargs)

        with (
            patch(
                "media_mate.proxy.subprocess.run",
                side_effect=capturing_ffmpeg,
            ),
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_probe.return_value = (1280, 720, 60.0)
            generate_proxies(src, out, store, config=cfg)
            args = captured_args[0]
            # Args: [ffmpeg, -y, -i, src, -vf, vf_str, -c:v, codec, -profile:v, profile, -c:a, audio, out]
            # ProRes422HQ is profile 3, height 720
            assert args[5] == "scale=-2:720"
            assert args[9] == "3"  # ProRes422HQ profile

    def test_run_logs_proxy_path(self, tmp_path: Path, store_dir: Path) -> None:
        src = tmp_path / "in"
        src.mkdir()
        (src / "clip.mov").write_bytes(b"x")
        out = tmp_path / "out"
        store = _make_store(store_dir)

        with (
            patch(
                "media_mate.proxy.subprocess.run",
                side_effect=_fake_successful_ffmpeg,
            ),
            patch("media_mate.proxy._probe_output_metadata") as mock_probe,
        ):
            mock_probe.return_value = (1920, 1080, 60.0)
            generate_proxies(src, out, store)

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute("SELECT proxy_path, codec, height FROM proxies LIMIT 1").fetchone()
            assert row[0] == str(out / "clip.mov")
            assert row[1] == "ProRes422Proxy"
            assert row[2] == 1080
