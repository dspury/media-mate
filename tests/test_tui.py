"""Focused tests for the Textual workstation and config persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

from media_mate.config import load_config
from media_mate.models import ChecksumAlgo, MediaMateConfig, OrganizeResult
from media_mate.tui import (
    HomeScreen,
    LogScreen,
    MediaMateApp,
    PipelineOptions,
    PipelineScreen,
    QueueItem,
    SettingsScreen,
    _drive_label,
    _format_size,
    compute_output_tree,
    list_external_drives,
    save_config,
)


def test_save_config_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "media-mate.toml"
    expected = MediaMateConfig(
        proxy_codec="ProRes422HQ",
        proxy_height=720,
        checksum_algo=ChecksumAlgo.SHA256,
        ffmpeg_path="/opt/tools/ffmpeg",
    )
    save_config(expected, target)
    actual = load_config(target)
    assert actual == expected
    assert not target.with_suffix(".toml.tmp").exists()


def test_save_config_preserves_comments_and_layout(tmp_path: Path) -> None:
    target = tmp_path / "media-mate.toml"
    target.write_text(
        "# Edit-suite overrides\n"
        "proxy_height = 1080 # offline default\n\n"
        "[organize]\n"
        "# Keep card folders intact\n"
        'mode = "copy"\n'
    )

    save_config(MediaMateConfig(proxy_height=720), target)

    saved = target.read_text()
    assert "# Edit-suite overrides" in saved
    assert "# offline default" in saved
    assert "# Keep card folders intact" in saved
    assert "proxy_height = 720" in saved
    assert saved.count("[organize]") == 1


def test_keyboard_screen_navigation(tmp_path: Path) -> None:
    async def navigate() -> None:
        app = MediaMateApp(tmp_path / "audit.db")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, PipelineScreen)
            for control in (
                "#output-root",
                "#move",
                "#dry-run",
                "#accept-changes",
                "#project-name",
                "#resolution",
                "#frame-rate",
                "#color-space",
            ):
                app.screen.query_one(control)
            await pilot.press("escape", "l")
            await pilot.pause()
            assert isinstance(app.screen, LogScreen)
            await pilot.press("escape", "s")
            await pilot.pause()
            assert isinstance(app.screen, SettingsScreen)

    asyncio.run(navigate())


# ---------------------------------------------------------------------------
# Multi-folder output-isolation regression test (review: shared output root)
# ---------------------------------------------------------------------------


class TestComputeOutputTree:
    def test_shared_root_isolates_per_source(self, tmp_path: Path) -> None:
        root = tmp_path / "jobs"
        card_a = tmp_path / "card_a"
        card_b = tmp_path / "card_b"
        assert compute_output_tree(root, card_a) == root / "card_a"
        assert compute_output_tree(root, card_b) == root / "card_b"
        assert compute_output_tree(root, card_a) != compute_output_tree(root, card_b)

    def test_same_named_clips_land_in_separate_trees(self, tmp_path: Path) -> None:
        """The review's exact collision: two folders both containing clip.MP4."""
        root = tmp_path / "jobs"
        card_a = tmp_path / "card_a"
        card_b = tmp_path / "card_b"
        a_organized = compute_output_tree(root, card_a) / "organized"
        b_organized = compute_output_tree(root, card_b) / "organized"
        assert a_organized / "clip.mov" != b_organized / "clip.mov"

    def test_no_root_uses_source_parent(self, tmp_path: Path) -> None:
        card = tmp_path / "card"
        assert compute_output_tree(None, card) == tmp_path / "card"


def _options(output_root: Path | None, dry_run: bool = False) -> PipelineOptions:
    return PipelineOptions(
        output_root=output_root,
        move=False,
        dry_run=dry_run,
        accept_changes=False,
        project_name="proj",
        resolution="1080",
        frame_rate="24",
        color_space="Rec.709",
    )


def test_run_queue_isolates_multi_folder_outputs(tmp_path: Path) -> None:
    """Two sources sharing an output root must get separate trees (no collision)."""
    card_a = tmp_path / "card_a"
    card_b = tmp_path / "card_b"
    card_a.mkdir()
    card_b.mkdir()
    (card_a / "clip.mov").write_bytes(b"a" * 128)
    (card_b / "clip.mov").write_bytes(b"b" * 128)
    out_root = tmp_path / "jobs"
    db = tmp_path / "audit.db"
    cfg_path = tmp_path / "media-mate.toml"

    screen = PipelineScreen()
    screen.items = [QueueItem(card_a), QueueItem(card_b)]
    screen.cancel_requested = False
    screen.started = 0.0
    fake_app = SimpleNamespace(
        db_path=db,
        config_path=cfg_path,
        call_from_thread=lambda *a, **k: None,
    )

    # Record the dest_root each organize call receives. Before the fix both
    # calls received the SAME <root>/organized; after, they are per-source.
    seen_roots: list[Path] = []

    def fake_organize(source, dest_root, store, **kwargs):
        seen_roots.append(Path(dest_root))
        return OrganizeResult(
            source_path=str(source),
            destination_root=str(dest_root),
            files_moved=1,
            files_skipped=0,
            bytes_moved=128,
            duration_seconds=0.0,
            dry_run=kwargs.get("dry_run", False),
        )

    with (
        patch.object(PipelineScreen, "app", new_callable=PropertyMock, return_value=fake_app),
        patch("media_mate.organize.organize_path", side_effect=fake_organize),
    ):
        screen._run_queue(["organize"], _options(out_root))

    assert len(seen_roots) == 2
    assert seen_roots[0] != seen_roots[1]
    assert seen_roots[0] == out_root / "card_a" / "organized"
    assert seen_roots[1] == out_root / "card_b" / "organized"
    # Both queue items completed.
    assert all(item.status == "done" for item in screen.items)


def test_run_queue_skips_downstream_when_organize_is_dry_run(tmp_path: Path) -> None:
    """Dry-run organize must not poison proxy/verify with an unpopulated tree."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "clip.mov").write_bytes(b"x" * 128)
    out_root = tmp_path / "jobs"
    db = tmp_path / "audit.db"
    cfg_path = tmp_path / "media-mate.toml"

    screen = PipelineScreen()
    screen.items = [QueueItem(card)]
    screen.cancel_requested = False
    screen.started = 0.0
    fake_app = SimpleNamespace(
        db_path=db, config_path=cfg_path, call_from_thread=lambda *a, **k: None
    )

    proxy_calls = {"n": 0}
    verify_calls = {"n": 0}

    def fake_proxy(*a, **k):
        proxy_calls["n"] += 1
        return MagicMock()

    def fake_verify(*a, **k):
        verify_calls["n"] += 1
        return MagicMock()

    with (
        patch.object(PipelineScreen, "app", new_callable=PropertyMock, return_value=fake_app),
        patch("media_mate.proxy.generate_proxies", side_effect=fake_proxy),
        patch("media_mate.verify.verify_folder", side_effect=fake_verify),
    ):
        screen._run_queue(["organize", "proxy", "verify"], _options(out_root, dry_run=True))

    # Organize ran (dry-run) but proxy + verify were skipped — not poisoned.
    assert proxy_calls["n"] == 0
    assert verify_calls["n"] == 0
    assert screen.items[0].status == "done"


# ---------------------------------------------------------------------------
# External-drive detection (review: TUI surfacing of /Volumes, /media, etc.)
# ---------------------------------------------------------------------------


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(512) == "512.0B"

    def test_kilobytes(self) -> None:
        assert _format_size(2048) == "2.0K"

    def test_megabytes(self) -> None:
        assert _format_size(5 * 1024 * 1024) == "5.0M"

    def test_gigabytes(self) -> None:
        assert _format_size(3 * 1024**3) == "3.0G"

    def test_terabytes(self) -> None:
        assert _format_size(2 * 1024**4) == "2.0T"

    def test_petabytes_and_beyond(self) -> None:
        assert _format_size(5 * 1024**5) == "5.0P"


class TestListExternalDrives:
    """Cross-platform drive detection — macOS, Linux, Windows branches."""

    def test_macos_lists_volumes_but_excludes_system_root(self, tmp_path: Path) -> None:
        # Build a fake /Volumes tree: one system volume that resolves to /
        # (via symlink, so Path.resolve() really matches Path("/").resolve()),
        # plus two removable drives that resolve to themselves.
        fake_root = tmp_path / "Volumes"
        fake_root.mkdir()

        # A separate directory that "Macintosh HD" will resolve to; mimics how
        # macOS exposes the system volume as an alias whose realpath is "/".
        real_root = tmp_path / "_root"
        real_root.mkdir()
        (fake_root / "Macintosh HD").symlink_to(real_root)
        (fake_root / "Camera Card").mkdir()
        (fake_root / "Backup").mkdir()
        # Non-directory entries should be ignored.
        (fake_root / "garbage.txt").write_text("not a drive")

        # Redirect the helper's "real" / and /Volumes to our fake tree, but
        # leave Path() itself intact so .is_dir() / .resolve() / .iterdir()
        # all run on real Path instances.
        def fake_path_factory(arg: object) -> Path:
            text = str(arg)
            if text == "/Volumes":
                return fake_root
            if text == "/":
                return real_root
            return Path(text)

        with (
            patch("media_mate.tui.Path", side_effect=fake_path_factory),
            patch("media_mate.tui.platform.system", return_value="Darwin"),
        ):
            drives = list_external_drives()

        names = {d.name for d in drives}
        assert names == {"Camera Card", "Backup"}, f"system volume should be excluded; got {names}"
        # And it should specifically NOT be the system volume.
        assert all("Macintosh" not in d.name for d in drives)

    def test_linux_dedups_user_media_paths(self, tmp_path: Path) -> None:
        user = "alice"
        user_media = tmp_path / "media" / user
        user_media.mkdir(parents=True)
        run_media = tmp_path / "run" / "media" / user
        run_media.mkdir(parents=True)

        card = user_media / "SDCARD"
        card.mkdir()
        backup = run_media / "BACKUP"
        backup.mkdir()
        # Same drive mounted via both paths (realpath collision) — must dedup.
        (user_media / "OVERLAP").mkdir()
        (run_media / "OVERLAP").symlink_to(tmp_path / "media" / user / "OVERLAP")

        with (
            patch.dict("os.environ", {"USER": user}, clear=False),
            patch("media_mate.tui.Path") as path_cls,
        ):
            # Map exact strings (no substring matching — /media/alice is a
            # suffix of /run/media/alice, so naive .replace() corrupts it).
            def factory(arg: object) -> Path:
                text = str(arg)
                if text == "/run/media/alice":
                    return run_media
                if text == "/media/alice":
                    return user_media
                return Path(text)

            path_cls.side_effect = factory

            with patch("media_mate.tui.platform.system", return_value="Linux"):
                drives = list_external_drives()

        names = sorted(d.name for d in drives)
        assert names == ["BACKUP", "OVERLAP", "SDCARD"]
        # OVERLAP appears only once despite the symlink collision.
        assert sum(d.name == "OVERLAP" for d in drives) == 1

    def test_linux_returns_empty_when_no_user_media_dir(self, tmp_path: Path) -> None:
        with (
            patch("media_mate.tui.platform.system", return_value="Linux"),
            patch("media_mate.tui.Path", side_effect=lambda p: Path(str(p))),
        ):
            drives = list_external_drives()
        assert drives == []

    def test_windows_skips_system_drive(self) -> None:
        # SYSTEMDRIVE is normally C:; the helper should skip it. On POSIX we
        # can't inspect .drive (PosixPath has no concept), so compare the
        # string form of the path the helper constructs.
        available = {"D:", "E:"}

        def fake_stat(self: Path) -> bool:
            return str(self).rstrip("\\").rstrip("/") in available

        with (
            patch.dict("os.environ", {"SYSTEMDRIVE": "C:"}, clear=False),
            patch("media_mate.tui.platform.system", return_value="Windows"),
            patch.object(Path, "exists", fake_stat),
            patch.object(Path, "is_dir", fake_stat),
        ):
            drives = list_external_drives()

        names = sorted(str(d).rstrip("\\").rstrip("/") for d in drives)
        assert names == ["D:", "E:"]

    def test_returns_empty_for_unknown_platform(self) -> None:
        with patch("media_mate.tui.platform.system", return_value="Plan9"):
            assert list_external_drives() == []

    def test_drive_label_falls_back_when_disk_usage_fails(self, tmp_path: Path) -> None:
        # disk_usage raises (e.g. drive unmounted between detection and display).
        with patch("media_mate.tui.shutil.disk_usage", side_effect=OSError("not mounted")):
            label = _drive_label(tmp_path / "Card")
        assert label == "Card"

    def test_drive_label_includes_free_and_total(self, tmp_path: Path) -> None:
        drive = tmp_path / "Camera Card"
        drive.mkdir()
        with patch(
            "media_mate.tui.shutil.disk_usage",
            return_value=SimpleNamespace(free=552 * 1024**3, total=1800 * 1024**3),
        ):
            label = _drive_label(drive)
        assert label.startswith("Camera Card")
        assert "free" in label and "/" in label

    def test_pipeline_screen_shows_detected_drives(self, tmp_path: Path) -> None:
        """The background scan populates self.drives and reveals the drives list."""
        from textual.widgets import OptionList

        fake_drives = [Path("/Volumes/Card"), Path("/Volumes/Backup")]

        async def run() -> None:
            app = MediaMateApp(tmp_path / "audit.db")
            with patch("media_mate.tui.list_external_drives", return_value=fake_drives):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.press("r")
                    screen = app.screen
                    assert isinstance(screen, PipelineScreen)
                    # The scan runs in a thread worker — poll until it lands.
                    for _ in range(100):
                        await pilot.pause(0.05)
                        if screen.drives == fake_drives:
                            break
                    assert screen.drives == fake_drives
                    drive_list = screen.query_one("#drives-list", OptionList)
                    assert drive_list.display
                    assert drive_list.option_count == 2

        asyncio.run(run())

    def test_pipeline_screen_hides_drives_widget_when_none_detected(self, tmp_path: Path) -> None:
        """When no external drives are connected, the drives section stays hidden."""

        async def run() -> None:
            app = MediaMateApp(tmp_path / "audit.db")
            with patch("media_mate.tui.list_external_drives", return_value=[]):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.press("r")
                    screen = app.screen
                    assert isinstance(screen, PipelineScreen)
                    for _ in range(20):
                        await pilot.pause(0.05)
                    assert not screen.query_one("#drives-list").display
                    assert not screen.query_one("#drives-label").display

        asyncio.run(run())
