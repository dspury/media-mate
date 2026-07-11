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
    compute_output_tree,
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
