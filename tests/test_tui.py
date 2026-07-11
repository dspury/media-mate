"""Focused tests for the Textual workstation and config persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

from media_mate.config import load_config
from media_mate.models import ChecksumAlgo, MediaMateConfig
from media_mate.tui import (
    HomeScreen,
    LogScreen,
    MediaMateApp,
    PipelineScreen,
    SettingsScreen,
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
