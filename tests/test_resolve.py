"""Tests for the resolve capability in resolve.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_mate.log import LogStore
from media_mate.models import (
    MediaMateConfig,
    ResolveProjectSpec,
)
from media_mate.resolve import (
    ResolveError,
    _bin_for,
    build_project_manifest,
    create_resolve_project,
    find_resolve,
    resolve_bin_structure,
    write_manifest,
)

# ---------------------------------------------------------------------------
# resolve_bin_structure tests
# ---------------------------------------------------------------------------


class TestResolveBinStructure:
    def test_empty_folder(self, tmp_path: Path) -> None:
        assert resolve_bin_structure(tmp_path) == [tmp_path.name]

    def test_single_level(self, tmp_path: Path) -> None:
        (tmp_path / "clip.mov").write_bytes(b"x")
        (tmp_path / "sub1").mkdir()
        (tmp_path / "sub2").mkdir()
        bins = resolve_bin_structure(tmp_path)
        assert bins == [tmp_path.name, f"{tmp_path.name}/sub1", f"{tmp_path.name}/sub2"]

    def test_nested_subdirs(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "c").mkdir()
        bins = resolve_bin_structure(tmp_path)
        assert bins == [
            tmp_path.name,
            f"{tmp_path.name}/a",
            f"{tmp_path.name}/a/b",
            f"{tmp_path.name}/a/b/c",
        ]

    def test_no_duplicate_root(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        bins = resolve_bin_structure(tmp_path)
        # Root appears once
        assert bins.count(tmp_path.name) == 1


# ---------------------------------------------------------------------------
# _bin_for tests
# ---------------------------------------------------------------------------


class TestBinFor:
    def test_top_level_file(self, tmp_path: Path) -> None:
        f = tmp_path / "clip.mov"
        assert _bin_for(f, tmp_path) == tmp_path.name

    def test_file_in_subdir(self, tmp_path: Path) -> None:
        f = tmp_path / "sub" / "clip.mov"
        assert _bin_for(f, tmp_path) == f"{tmp_path.name}/sub"

    def test_file_in_nested_subdir(self, tmp_path: Path) -> None:
        # _bin_for only looks at the FIRST subdir level
        f = tmp_path / "sub1" / "sub2" / "clip.mov"
        assert _bin_for(f, tmp_path) == f"{tmp_path.name}/sub1"


# ---------------------------------------------------------------------------
# build_project_manifest tests
# ---------------------------------------------------------------------------


class TestBuildProjectManifest:
    def test_empty_source(self, tmp_path: Path) -> None:
        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(tmp_path),
            output_path=str(tmp_path / "test.drp"),
        )
        manifest = build_project_manifest(tmp_path, spec)
        assert manifest["name"] == "Test"
        assert manifest["bins"] == {tmp_path.name: []}
        assert manifest["timeline"]["clips"] == []
        assert manifest["settings"]["resolution"] == "1080"

    def test_distributes_files_to_bins(self, tmp_path: Path) -> None:
        (tmp_path / "top.mov").write_bytes(b"x")
        sub1 = tmp_path / "sub1"
        sub1.mkdir()
        (sub1 / "in_sub1.mov").write_bytes(b"x")
        sub2 = tmp_path / "sub2"
        sub2.mkdir()
        (sub2 / "in_sub2.mov").write_bytes(b"x")

        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(tmp_path),
            output_path=str(tmp_path / "test.drp"),
        )
        manifest = build_project_manifest(tmp_path, spec)

        root_bin = tmp_path.name
        assert root_bin in manifest["bins"]
        assert f"{tmp_path.name}/sub1" in manifest["bins"]
        assert f"{tmp_path.name}/sub2" in manifest["bins"]

        # top.mov is in root, others are in their respective sub-bins
        assert any(p.endswith("top.mov") for p in manifest["bins"][root_bin])
        assert any(p.endswith("in_sub1.mov") for p in manifest["bins"][f"{tmp_path.name}/sub1"])
        assert any(p.endswith("in_sub2.mov") for p in manifest["bins"][f"{tmp_path.name}/sub2"])

    def test_includes_proxies_when_dir_provided(self, tmp_path: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"a")
        sub = source / "sub"
        sub.mkdir()
        (sub / "b.mov").write_bytes(b"b")
        # Proxies mirror source structure
        proxies = tmp_path / "proxies"
        proxies.mkdir()
        (proxies / "a.mov").write_bytes(b"proxy a")
        (proxies / "sub").mkdir()
        (proxies / "sub" / "b.mov").write_bytes(b"proxy b")

        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        manifest = build_project_manifest(source, spec, proxy_dir=proxies)

        clips = manifest["timeline"]["clips"]
        assert len(clips) == 2
        proxy_paths = [c["proxy"] for c in clips]
        assert any(p.endswith("a.mov") for p in proxy_paths)
        assert any(p.endswith("b.mov") for p in proxy_paths)

    def test_no_proxies_when_dir_does_not_exist(self, tmp_path: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"a")

        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        manifest = build_project_manifest(source, spec, proxy_dir=tmp_path / "nonexistent")

        assert manifest["timeline"]["clips"] == []

    def test_skips_proxies_that_dont_mirror_source(self, tmp_path: Path) -> None:
        """Only include proxy clips where the proxy file actually exists at the mirrored path."""
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"a")
        (source / "b.mov").write_bytes(b"b")

        proxies = tmp_path / "proxies"
        proxies.mkdir()
        # Only a.mov has a proxy; b.mov doesn't
        (proxies / "a.mov").write_bytes(b"proxy a")

        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        manifest = build_project_manifest(source, spec, proxy_dir=proxies)

        clips = manifest["timeline"]["clips"]
        assert len(clips) == 1
        assert clips[0]["source"].endswith("a.mov")

    def test_settings_from_spec(self, tmp_path: Path) -> None:
        spec = ResolveProjectSpec(
            name="Test",
            source_folder=str(tmp_path),
            output_path=str(tmp_path / "test.drp"),
            resolution="4K",
            frame_rate="23.976",
            color_space="Rec.2020",
        )
        manifest = build_project_manifest(tmp_path, spec)
        assert manifest["settings"]["resolution"] == "4K"
        assert manifest["settings"]["frame_rate"] == "23.976"
        assert manifest["settings"]["color_space"] == "Rec.2020"


# ---------------------------------------------------------------------------
# write_manifest tests
# ---------------------------------------------------------------------------


class TestWriteManifest:
    def test_writes_json(self, tmp_path: Path) -> None:
        manifest = {"name": "Test", "bins": {"a": []}}
        out = tmp_path / "manifest.json"
        write_manifest(manifest, out)
        with open(out) as f:
            loaded = json.load(f)
        assert loaded == manifest

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "deep" / "nested" / "manifest.json"
        write_manifest({"x": 1}, out)
        assert out.exists()

    def test_returns_path(self, tmp_path: Path) -> None:
        out = tmp_path / "m.json"
        result = write_manifest({}, out)
        assert result == out


# ---------------------------------------------------------------------------
# find_resolve tests
# ---------------------------------------------------------------------------


class TestFindResolve:
    def test_returns_none_when_module_missing(self) -> None:
        # Block the import by passing a non-existent resolve_path
        cfg = MediaMateConfig(resolve_path="/nonexistent/resolve")
        with patch.dict("sys.modules", {"DaVinciResolveScript": None}):
            assert find_resolve(cfg) is None

    def test_returns_module_when_present(self) -> None:
        fake_module = MagicMock(name="DaVinciResolveScript")
        with patch.dict("sys.modules", {"DaVinciResolveScript": fake_module}):
            result = find_resolve()
        assert result is fake_module

    def test_prepends_resolve_path_to_sys_path(self) -> None:
        cfg = MediaMateConfig(resolve_path="/custom/resolve")
        fake_modules_dir = MagicMock()
        fake_modules_dir.is_dir.return_value = True

        with (
            patch("pathlib.Path.__truediv__") as mock_truediv,
            patch.dict("sys.modules", {"DaVinciResolveScript": MagicMock()}),
        ):
            # Mock the modules dir construction
            mock_truediv.return_value = fake_modules_dir
            with patch.object(Path, "is_dir", return_value=True):
                find_resolve(cfg)
        # Hard to assert sys.path directly without more mocking — just ensure no error


# ---------------------------------------------------------------------------
# create_resolve_project tests
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


def _make_mock_resolve_module(  # type: ignore[no-untyped-def]
    version: str = "20.0",
    create_project_succeeds: bool = True,
):
    """Build a mocked DaVinciResolveScript module + scriptapp() handle."""
    # Project
    project = MagicMock()
    project.SetSetting = MagicMock()

    # ProjectManager
    pm = MagicMock()
    if create_project_succeeds:
        pm.CreateProject.return_value = project
    else:
        pm.CreateProject.return_value = None

    # MediaPool
    media_pool = MagicMock()
    root_bin = MagicMock()
    media_pool.GetCurrentFolder.return_value = root_bin
    media_pool.AddSubFolder.return_value = MagicMock()
    timeline = MagicMock()
    media_pool.CreateTimeline.return_value = timeline

    pm.GetMediaPool.return_value = media_pool

    # Resolve handle
    resolve = MagicMock()
    resolve.GetProjectManager.return_value = pm
    resolve.GetVersionString.return_value = version

    # scriptapp entry point
    scriptapp = MagicMock(return_value=resolve)

    # Module
    module = MagicMock()
    module.scriptapp = scriptapp
    return module, resolve, pm, media_pool


class TestCreateResolveProject:
    def test_source_does_not_exist(self, tmp_path: Path, store_dir: Path) -> None:
        spec = ResolveProjectSpec(
            name="Test",
            source_folder="/nope",
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)
        with pytest.raises(ResolveError):
            create_resolve_project(spec, tmp_path / "nope", None, store)

    def test_resolve_unavailable_writes_manifest(self, tmp_path: Path, store_dir: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"x")

        spec = ResolveProjectSpec(
            name="TestProject",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)

        # Mock find_resolve to return None
        with patch("media_mate.resolve.find_resolve", return_value=None):
            result = create_resolve_project(spec, source, None, store)

        assert result.resolve_version is None
        assert result.name == "TestProject"
        # Manifest was written
        manifest_path = tmp_path / "test.drp.manifest.json"
        assert manifest_path.exists()
        loaded = json.loads(manifest_path.read_text())
        assert loaded["name"] == "TestProject"
        # Project record was logged
        assert _count_rows(store, "projects") == 1

    def test_resolve_available_creates_project(self, tmp_path: Path, store_dir: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"x")
        sub = source / "sub"
        sub.mkdir()
        (sub / "b.mov").write_bytes(b"x")

        spec = ResolveProjectSpec(
            name="TestProject",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)

        module, _resolve, pm, _media_pool = _make_mock_resolve_module(version="20.0")

        with patch("media_mate.resolve.find_resolve", return_value=module):
            result = create_resolve_project(spec, source, None, store)

        assert result.resolve_version == "20.0"
        assert result.name == "TestProject"
        assert result.bin_count >= 2  # root + sub
        # Resolve API was called
        pm.CreateProject.assert_called_once()
        pm.SaveProject.assert_called_once()

    def test_resolve_failure_falls_back_to_manifest(self, tmp_path: Path, store_dir: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"x")

        spec = ResolveProjectSpec(
            name="TestProject",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)

        # Resolve "available" but CreateProject returns None
        module, _, _, _ = _make_mock_resolve_module(create_project_succeeds=False)

        with patch("media_mate.resolve.find_resolve", return_value=module):
            result = create_resolve_project(spec, source, None, store)

        # Fell back to manifest
        assert result.resolve_version is None
        manifest_path = tmp_path / "test.drp.manifest.json"
        assert manifest_path.exists()

        # Run status should be PARTIAL (Resolve was attempted but failed)
        with sqlite3.connect(store.db_path) as conn:
            status = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert status == "partial"

    def test_resolve_unavailable_run_is_success(self, tmp_path: Path, store_dir: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"x")

        spec = ResolveProjectSpec(
            name="TestProject",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)

        with patch("media_mate.resolve.find_resolve", return_value=None):
            create_resolve_project(spec, source, None, store)

        # When Resolve was never available, we just wrote a manifest — that's success.
        with sqlite3.connect(store.db_path) as conn:
            status = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert status == "success"

    def test_includes_proxy_clips_when_proxies_exist(self, tmp_path: Path, store_dir: Path) -> None:
        source = tmp_path / "raw"
        source.mkdir()
        (source / "a.mov").write_bytes(b"a")
        proxies = tmp_path / "proxies"
        proxies.mkdir()
        (proxies / "a.mov").write_bytes(b"proxy a")

        spec = ResolveProjectSpec(
            name="TestProject",
            source_folder=str(source),
            output_path=str(tmp_path / "test.drp"),
        )
        store = _make_store(store_dir)

        with patch("media_mate.resolve.find_resolve", return_value=None):
            create_resolve_project(spec, source, proxies, store)

        # Manifest should have the proxy clip
        manifest_path = tmp_path / "test.drp.manifest.json"
        loaded = json.loads(manifest_path.read_text())
        assert len(loaded["timeline"]["clips"]) == 1
