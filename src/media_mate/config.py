"""Configuration loader for media-mate.

Loads a media-mate.toml file into a MediaMateConfig pydantic model. When no
config file is found, returns MediaMateConfig() (all defaults).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from media_mate.models import MediaMateConfig


def load_config(path: Path | None = None) -> MediaMateConfig:
    """Load config from a TOML file.

    Search order:
    1. Explicit path argument (if provided)
    2. ./media-mate.toml
    3. ~/.media-mate/config.toml
    4. Defaults (MediaMateConfig())

    Missing files are not errors — defaults are returned.
    Invalid TOML raises ValueError so the CLI can surface it cleanly.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.append(Path.cwd() / "media-mate.toml")
        home_config = Path.home() / ".media-mate" / "config.toml"
        if home_config.is_file():
            candidates.append(home_config)

    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, "rb") as f:
                data = tomllib.load(f)
            return MediaMateConfig.model_validate(data)

    return MediaMateConfig()


__all__ = ["load_config"]
