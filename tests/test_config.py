"""Tests for the configuration loader in config.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_mate.config import load_config
from media_mate.models import ChecksumAlgo, MediaMateConfig


class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path: Path, monkeypatch) -> None:
        # Change cwd so ./media-mate.toml doesn't accidentally exist
        monkeypatch.chdir(tmp_path)
        # And patch home so ~/.media-mate/config.toml doesn't exist
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        cfg = load_config()
        assert isinstance(cfg, MediaMateConfig)
        assert cfg.proxy_codec == "ProRes422Proxy"
        assert cfg.proxy_height == 1080
        assert cfg.checksum_algo == ChecksumAlgo.XXHASH

    def test_loads_explicit_path(self, tmp_path: Path) -> None:
        path = tmp_path / "media-mate.toml"
        path.write_text(
            """
proxy_codec = "ProRes422HQ"
proxy_height = 720
checksum_algo = "sha256"
"""
        )
        cfg = load_config(path)
        assert cfg.proxy_codec == "ProRes422HQ"
        assert cfg.proxy_height == 720
        assert cfg.checksum_algo == ChecksumAlgo.SHA256

    def test_loads_organize_section(self, tmp_path: Path) -> None:
        path = tmp_path / "media-mate.toml"
        path.write_text(
            """
[organize]
template = "{root}/{filename}{ext}"
on_conflict = "rename"
"""
        )
        cfg = load_config(path)
        assert cfg.organize.template == "{root}/{filename}{ext}"
        assert cfg.organize.on_conflict == "rename"

    def test_loads_resolve_path(self, tmp_path: Path) -> None:
        path = tmp_path / "media-mate.toml"
        path.write_text(
            """
resolve_path = "/Applications/DaVinci Resolve.app"
ffmpeg_path = "/opt/homebrew/bin/ffmpeg"
"""
        )
        cfg = load_config(path)
        assert cfg.resolve_path == "/Applications/DaVinci Resolve.app"
        assert cfg.ffmpeg_path == "/opt/homebrew/bin/ffmpeg"

    def test_extra_fields_rejected(self, tmp_path: Path) -> None:
        """MediaMateConfig has extra='forbid' so typos blow up."""
        path = tmp_path / "media-mate.toml"
        path.write_text('unknown_field = "boom"\n')
        with pytest.raises(ValueError):
            load_config(path)

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "media-mate.toml"
        path.write_text("this is not valid = toml = syntax [[[")
        with pytest.raises(ValueError):
            load_config(path)

    def test_default_search_finds_cwd_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "media-mate.toml").write_text("proxy_height = 540\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-such-home")
        cfg = load_config()
        assert cfg.proxy_height == 540

    def test_default_search_finds_home_config(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / ".media-mate").mkdir()
        (home / ".media-mate" / "config.toml").write_text("proxy_height = 480\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        cfg = load_config()
        assert cfg.proxy_height == 480

    def test_explicit_path_takes_precedence_over_cwd(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "media-mate.toml").write_text("proxy_height = 100\n")
        explicit = tmp_path / "override.toml"
        explicit.write_text("proxy_height = 999\n")
        cfg = load_config(explicit)
        assert cfg.proxy_height == 999

    def test_invalid_enum_value_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "media-mate.toml"
        path.write_text('checksum_algo = "not-a-real-algo"\n')
        with pytest.raises(ValueError):
            load_config(path)
