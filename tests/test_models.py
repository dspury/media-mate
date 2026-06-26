"""Tests for the pydantic models in models.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from media_mate.models import (
    ChecksumAlgo,
    MediaMateConfig,
    MediaProbe,
    OrganizeConfig,
    OrganizeOpRecord,
    OrganizeResult,
    ProxyRequest,
    ResolveProjectSpec,
    RunStatus,
    VerificationReport,
)


class TestMediaProbe:
    def test_minimal_construction(self) -> None:
        p = MediaProbe(path="/tmp/clip.mov")
        assert p.path == "/tmp/clip.mov"
        assert p.video_codec is None
        assert p.width is None
        assert p.height is None
        assert p.frame_rate is None
        assert isinstance(p.probed_at, datetime)

    def test_full_construction(self) -> None:
        p = MediaProbe(
            path="/tmp/clip.mov",
            container="mov",
            video_codec="h264",
            width=1920,
            height=1080,
            frame_rate=23.976,
            color_space="bt709",
            color_transfer="bt709",
            color_primaries="bt709",
            bit_depth=8,
            audio_codec="aac",
            audio_channels=2,
            audio_sample_rate=48000,
            audio_bit_depth=24,
            duration_seconds=120.5,
            file_size_bytes=104857600,
        )
        assert p.video_codec == "h264"
        assert p.width == 1920
        assert p.height == 1080
        assert p.duration_seconds == 120.5

    def test_is_frozen(self) -> None:
        p = MediaProbe(path="/tmp/clip.mov")
        with pytest.raises(ValidationError):
            p.path = "/tmp/other.mov"  # type: ignore[misc]

    def test_json_round_trip(self) -> None:
        p = MediaProbe(path="/tmp/clip.mov", width=1920, height=1080)
        as_json = p.model_dump_json()
        restored = MediaProbe.model_validate_json(as_json)
        assert restored == p


class TestOrganize:
    def test_default_config(self) -> None:
        cfg = OrganizeConfig()
        assert "{codec_family}" in cfg.template
        assert cfg.on_conflict == "skip"

    def test_custom_template(self) -> None:
        cfg = OrganizeConfig(template="{root}/{filename}{ext}")
        assert cfg.template == "{root}/{filename}{ext}"

    def test_invalid_on_conflict_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OrganizeConfig(on_conflict="delete_everything")  # type: ignore[arg-type]

    def test_organize_result_defaults(self) -> None:
        r = OrganizeResult(
            source_path="/in",
            destination_root="/out",
            files_moved=5,
            files_skipped=1,
            bytes_moved=1000,
            duration_seconds=1.5,
            dry_run=False,
        )
        assert r.errors == []
        assert r.files_moved == 5

    def test_organize_op_record(self) -> None:
        r = OrganizeOpRecord(
            run_id=1,
            source_path="/in/clip.mov",
            destination_path="/out/prores/1080p/clip.mov",
            codec_family="prores",
            resolution_bucket="1080p",
            file_size=1024,
            moved_at=datetime.now(timezone.utc),
        )
        assert r.id is None
        assert r.codec_family == "prores"


class TestMediaMateConfig:
    def test_defaults(self) -> None:
        cfg = MediaMateConfig()
        assert cfg.proxy_codec == "ProRes422Proxy"
        assert cfg.proxy_height == 1080
        assert cfg.checksum_algo == ChecksumAlgo.XXHASH
        assert cfg.resolve_path is None
        assert cfg.ffmpeg_path is None

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MediaMateConfig(unknown_field="boom")  # type: ignore[call-arg]

    def test_invalid_resolution_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ResolveProjectSpec(
                name="X", source_folder=".", output_path="/tmp/x.drp", resolution="1440"
            )  # type: ignore[arg-type]

    def test_invalid_fps_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ResolveProjectSpec(
                name="X", source_folder=".", output_path="/tmp/x.drp", frame_rate="100"
            )  # type: ignore[arg-type]


class TestEnums:
    def test_run_status_values(self) -> None:
        assert RunStatus.RUNNING.value == "running"
        assert RunStatus.SUCCESS.value == "success"
        assert RunStatus.FAILED.value == "failed"
        assert RunStatus.PARTIAL.value == "partial"

    def test_checksum_algo_values(self) -> None:
        assert ChecksumAlgo.XXHASH.value == "xxhash"
        assert ChecksumAlgo.SHA256.value == "sha256"


class TestVerificationReport:
    def test_is_clean(self) -> None:
        r = VerificationReport(
            folder="/tmp/raw",
            files_checked=10,
            files_missing=0,
            files_modified=0,
            files_added=0,
            checksum_algo=ChecksumAlgo.XXHASH,
            exit_code=0,
        )
        assert r.is_clean is True

    def test_is_not_clean_when_modified(self) -> None:
        r = VerificationReport(
            folder="/tmp/raw",
            files_checked=10,
            files_missing=0,
            files_modified=2,
            files_added=0,
            checksum_algo=ChecksumAlgo.XXHASH,
            exit_code=2,
        )
        assert r.is_clean is False


class TestProxyRequest:
    def test_defaults(self) -> None:
        req = ProxyRequest(source_path="/tmp/in.mov", output_path="/tmp/out.mov")
        assert req.codec == "ProRes422Proxy"
        assert req.target_height == 1080


def test_utc_now_is_aware() -> None:
    """Sanity check that pydantic default_factory uses timezone-aware UTC."""
    p = MediaProbe(path="/tmp/x.mov")
    assert p.probed_at.tzinfo is not None
    assert p.probed_at.tzinfo.utcoffset(p.probed_at).total_seconds() == 0  # type: ignore[union-attr]
