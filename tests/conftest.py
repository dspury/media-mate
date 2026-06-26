"""Shared pytest fixtures for media-mate tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: pytest.TempPathFactory) -> str:
    """Return a temporary directory path for test data."""
    return str(tmp_path)
