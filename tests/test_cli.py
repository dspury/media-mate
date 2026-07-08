"""Tests for the CLI in cli.py."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from media_mate.cli import _fetch_recent_runs, main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "media-mate.db"


def _invoke_with_db(runner: CliRunner, args: list[str], db: Path) -> object:
    """Invoke the CLI with a custom --db path."""
    return runner.invoke(main, ["--db", str(db), *args])


# ---------------------------------------------------------------------------
# Top-level: --version, --help
# ---------------------------------------------------------------------------


class TestTopLevel:
    def test_version(self, runner: CliRunner) -> None:
        from media_mate import __version__

        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "media-mate" in result.output
        assert __version__ in result.output

    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        # Subcommands should be listed
        for cmd in ("probe", "organize", "proxy", "resolve", "verify", "log", "run"):
            assert cmd in result.output


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


class TestProbeCommand:
    def test_probe_empty_dir(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        empty = tmp_path / "raw"
        empty.mkdir()
        result = _invoke_with_db(runner, ["probe", str(empty)], tmp_db)
        assert result.exit_code == 0
        # DB was created and initialized
        assert tmp_db.exists()

    def test_probe_with_files(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "a.mov").write_bytes(b"a")
        (raw / "b.mp4").write_bytes(b"b")

        with (
            patch("media_mate.cli.probe_path") as mock_probe,
        ):
            mock_probe.return_value = []  # empty for simplicity; we test wiring
            result = _invoke_with_db(runner, ["probe", str(raw)], tmp_db)

        assert result.exit_code == 0
        mock_probe.assert_called_once()
        args = mock_probe.call_args
        assert args[0][0] == raw  # path passed through
        # DB was created
        assert tmp_db.exists()

    def test_probe_missing_path_fails(self, runner: CliRunner, tmp_db: Path) -> None:
        result = _invoke_with_db(runner, ["probe", "/no/such/path"], tmp_db)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# organize
# ---------------------------------------------------------------------------


class TestOrganizeCommand:
    def test_organize(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        out = tmp_path / "out"
        (src / "a.mov").write_bytes(b"a")

        with patch("media_mate.cli.organize_path") as mock_org:
            from media_mate.models import OrganizeResult

            mock_org.return_value = OrganizeResult(
                source_path=str(src),
                destination_root=str(out),
                files_moved=1,
                files_skipped=0,
                bytes_moved=1024,
                duration_seconds=0.1,
                dry_run=False,
                errors=[],
            )
            result = _invoke_with_db(runner, ["organize", str(src), "--root", str(out)], tmp_db)

        assert result.exit_code == 0
        mock_org.assert_called_once()
        # Path and root passed through
        call_args = mock_org.call_args
        assert call_args[0][0] == src
        assert call_args[0][1] == out


# ---------------------------------------------------------------------------
# proxy
# ---------------------------------------------------------------------------


class TestProxyCommand:
    def test_proxy(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        out = tmp_path / "proxies"
        (src / "a.mov").write_bytes(b"a")

        with patch("media_mate.cli.generate_proxies") as mock_gen:
            from media_mate.models import ProxyBatchResult

            mock_gen.return_value = ProxyBatchResult()
            result = _invoke_with_db(runner, ["proxy", str(src), "--out", str(out)], tmp_db)

        assert result.exit_code == 0
        mock_gen.assert_called_once()
        call_args = mock_gen.call_args
        assert call_args[0][0] == src
        assert call_args[0][1] == out

    def test_proxy_missing_out_flag_fails(
        self, runner: CliRunner, tmp_path: Path, tmp_db: Path
    ) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        result = _invoke_with_db(runner, ["proxy", str(src)], tmp_db)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# resolve create
# ---------------------------------------------------------------------------


class TestResolveCommand:
    def test_resolve_create_when_resolve_available(
        self, runner: CliRunner, tmp_path: Path, tmp_db: Path
    ) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        (src / "a.mov").write_bytes(b"a")

        with patch("media_mate.cli.create_resolve_project") as mock_create:
            from datetime import datetime

            from media_mate.models import ResolveProjectResult

            mock_create.return_value = ResolveProjectResult(
                name="Test",
                path=str(src / "Test.drp"),
                resolution="1080",
                frame_rate="24",
                color_space="Rec.709",
                bin_count=2,
                timeline_count=1,
                resolve_version="20.0",
                created_at=datetime.now(UTC),
            )
            result = _invoke_with_db(
                runner,
                [
                    "resolve",
                    "create",
                    str(src),
                    "--project",
                    "Test",
                    "--resolution",
                    "1080",
                    "--fps",
                    "24",
                ],
                tmp_db,
            )

        assert result.exit_code == 0
        mock_create.assert_called_once()
        assert "Resolve 20.0" in result.output or "20.0" in result.output

    def test_resolve_create_when_unavailable_writes_manifest(
        self, runner: CliRunner, tmp_path: Path, tmp_db: Path
    ) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        (src / "a.mov").write_bytes(b"a")

        with patch("media_mate.cli.create_resolve_project") as mock_create:
            from datetime import datetime

            from media_mate.models import ResolveProjectResult

            mock_create.return_value = ResolveProjectResult(
                name="Test",
                path=str(src / "Test.drp"),
                resolution="1080",
                frame_rate="24",
                color_space="Rec.709",
                bin_count=2,
                timeline_count=1,
                resolve_version=None,
                created_at=datetime.now(UTC),
            )
            result = _invoke_with_db(
                runner,
                [
                    "resolve",
                    "create",
                    str(src),
                    "--project",
                    "Test",
                ],
                tmp_db,
            )

        assert result.exit_code == 0
        assert "manifest" in result.output.lower()

    def test_resolve_invalid_resolution(
        self, runner: CliRunner, tmp_path: Path, tmp_db: Path
    ) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        result = _invoke_with_db(
            runner,
            ["resolve", "create", str(src), "--project", "X", "--resolution", "1440"],
            tmp_db,
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerifyCommand:
    def test_verify_clean(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()

        with patch("media_mate.cli.verify_folder") as mock_v:
            from datetime import datetime

            from media_mate.models import ChecksumAlgo, VerificationReport

            mock_v.return_value = VerificationReport(
                folder=str(folder),
                files_checked=5,
                files_missing=0,
                files_modified=0,
                files_added=0,
                checksum_algo=ChecksumAlgo.XXHASH,
                verified_at=datetime.now(UTC),
                exit_code=0,
            )
            result = _invoke_with_db(runner, ["verify", str(folder)], tmp_db)

        assert result.exit_code == 0
        assert "Clean" in result.output

    def test_verify_missing_exits_1(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()

        with patch("media_mate.cli.verify_folder") as mock_v:
            from datetime import datetime

            from media_mate.models import ChecksumAlgo, VerificationReport

            mock_v.return_value = VerificationReport(
                folder=str(folder),
                files_checked=5,
                files_missing=1,
                files_modified=0,
                files_added=0,
                checksum_algo=ChecksumAlgo.XXHASH,
                verified_at=datetime.now(UTC),
                exit_code=1,
            )
            result = _invoke_with_db(runner, ["verify", str(folder)], tmp_db)

        assert result.exit_code == 1

    def test_verify_modified_exits_2(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()

        with patch("media_mate.cli.verify_folder") as mock_v:
            from datetime import datetime

            from media_mate.models import ChecksumAlgo, VerificationReport

            mock_v.return_value = VerificationReport(
                folder=str(folder),
                files_checked=5,
                files_missing=0,
                files_modified=1,
                files_added=0,
                checksum_algo=ChecksumAlgo.XXHASH,
                verified_at=datetime.now(UTC),
                exit_code=2,
            )
            result = _invoke_with_db(runner, ["verify", str(folder)], tmp_db)

        assert result.exit_code == 2

    def test_verify_added_exits_3(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        folder = tmp_path / "data"
        folder.mkdir()

        with patch("media_mate.cli.verify_folder") as mock_v:
            from datetime import datetime

            from media_mate.models import ChecksumAlgo, VerificationReport

            mock_v.return_value = VerificationReport(
                folder=str(folder),
                files_checked=5,
                files_missing=0,
                files_modified=0,
                files_added=1,
                checksum_algo=ChecksumAlgo.XXHASH,
                verified_at=datetime.now(UTC),
                exit_code=3,
            )
            result = _invoke_with_db(runner, ["verify", str(folder)], tmp_db)

        assert result.exit_code == 3


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestLogCommand:
    def test_log_empty(self, runner: CliRunner, tmp_db: Path) -> None:
        result = _invoke_with_db(runner, ["log"], tmp_db)
        assert result.exit_code == 0

    def test_log_json(self, runner: CliRunner, tmp_db: Path) -> None:
        result = _invoke_with_db(runner, ["log", "--format", "json"], tmp_db)
        assert result.exit_code == 0
        # Empty log returns '[]'
        assert "[]" in result.output or "[" in result.output

    def test_log_shows_runs(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        from media_mate.log import LogStore

        store = LogStore(tmp_db)
        store.initialize()
        store.start_run("media-mate probe ./raw")

        result = _invoke_with_db(runner, ["log"], tmp_db)
        assert result.exit_code == 0
        assert "media-mate probe" in result.output

    def test_log_limit(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        from media_mate.log import LogStore

        store = LogStore(tmp_db)
        store.initialize()
        for i in range(5):
            store.start_run(f"run {i}")

        result = _invoke_with_db(runner, ["log", "--limit", "2"], tmp_db)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# run (pipeline)
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_run_probe_only(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        (src / "a.mov").write_bytes(b"a")

        with patch("media_mate.cli.probe_path") as mock_probe:
            mock_probe.return_value = []
            result = _invoke_with_db(runner, ["run", str(src)], tmp_db)

        assert result.exit_code == 0
        # Probe was called
        mock_probe.assert_called_once()
        # Other capabilities were NOT called (no flags)
        assert "Step 1" in result.output
        assert "Step 2" not in result.output

    def test_run_with_all_steps(self, runner: CliRunner, tmp_path: Path, tmp_db: Path) -> None:
        src = tmp_path / "raw"
        src.mkdir()
        (src / "a.mov").write_bytes(b"a")

        with (
            patch("media_mate.cli.probe_path") as mock_probe,
            patch("media_mate.cli.organize_path") as mock_org,
            patch("media_mate.cli.generate_proxies") as mock_proxy,
            patch("media_mate.cli.create_resolve_project") as mock_resolve,
            patch("media_mate.cli.verify_folder") as mock_verify,
        ):
            from datetime import datetime

            from media_mate.models import (
                ChecksumAlgo,
                OrganizeResult,
                ProxyBatchResult,
                ResolveProjectResult,
                VerificationReport,
            )

            mock_probe.return_value = []
            mock_org.return_value = OrganizeResult(
                source_path=str(src),
                destination_root=str(src / "organized"),
                files_moved=1,
                files_skipped=0,
                bytes_moved=100,
                duration_seconds=0.1,
                dry_run=False,
                errors=[],
            )
            mock_proxy.return_value = ProxyBatchResult()
            mock_resolve.return_value = ResolveProjectResult(
                name="X",
                path="",
                resolution="1080",
                frame_rate="24",
                color_space="Rec.709",
                bin_count=1,
                timeline_count=0,
                resolve_version=None,
                created_at=datetime.now(UTC),
            )
            mock_verify.return_value = VerificationReport(
                folder=str(src),
                files_checked=1,
                files_missing=0,
                files_modified=0,
                files_added=0,
                checksum_algo=ChecksumAlgo.XXHASH,
                verified_at=datetime.now(UTC),
                exit_code=0,
            )

            result = _invoke_with_db(
                runner,
                [
                    "run",
                    str(src),
                    "--organize",
                    "--proxy",
                    "--resolve-project",
                    "--verify",
                    "--project-name",
                    "MyProject",
                ],
                tmp_db,
            )

        assert result.exit_code == 0
        # All capabilities were invoked
        mock_probe.assert_called_once()
        mock_org.assert_called_once()
        mock_proxy.assert_called_once()
        mock_resolve.assert_called_once()
        mock_verify.assert_called_once()
        # Output mentions all steps
        for step_label in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
            assert step_label in result.output


# ---------------------------------------------------------------------------
# _fetch_recent_runs
# ---------------------------------------------------------------------------


class TestFetchRecentRuns:
    def test_empty(self, tmp_db: Path) -> None:
        from media_mate.log import LogStore

        LogStore(tmp_db).initialize()
        assert _fetch_recent_runs(tmp_db, 10) == []

    def test_returns_recent(self, tmp_db: Path) -> None:
        from media_mate.log import LogStore

        store = LogStore(tmp_db)
        store.initialize()
        for i in range(3):
            store.start_run(f"run {i}")

        rows = _fetch_recent_runs(tmp_db, 10)
        assert len(rows) == 3
        # Most recent first
        assert rows[0]["command"] == "run 2"

    def test_limit_respected(self, tmp_db: Path) -> None:
        from media_mate.log import LogStore

        store = LogStore(tmp_db)
        store.initialize()
        for i in range(5):
            store.start_run(f"run {i}")

        rows = _fetch_recent_runs(tmp_db, 2)
        assert len(rows) == 2
