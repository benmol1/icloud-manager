from datetime import datetime, timezone
from pathlib import Path

from app.actions import (
    OffloadStatus,
    _destination_path,
    _offload_filename,
    _unique_destination,
    offload,
)
from app.analyser import ScoreBreakdown, ScoredAsset
from app.models import Asset, MediaType, Source


def _scored(
    asset_id: str = "abc123",
    filename: str = "IMG-0001.jpg",
    created: datetime | None = None,
    size_mb: float = 5.0,
) -> ScoredAsset:
    created = created or datetime(2023, 7, 9, 14, 30, tzinfo=timezone.utc)
    asset = Asset(
        asset_id=asset_id,
        filename=filename,
        size=int(size_mb * 1024 * 1024),
        created=created,
        media_type=MediaType.IMAGE,
        is_favorite=False,
        source=Source.WHATSAPP,
    )
    return ScoredAsset(asset=asset, breakdown=ScoreBreakdown())


class FakeSource:
    """In-memory AssetSource for exercising the live offload path."""

    def __init__(self, *, fail_download: bool = False, fail_delete: bool = False):
        self.fail_download = fail_download
        self.fail_delete = fail_delete
        self.downloaded: list[str] = []
        self.deleted: list[str] = []

    def download(self, asset: Asset) -> bytes:
        if self.fail_download:
            raise RuntimeError("download boom")
        self.downloaded.append(asset.asset_id)
        return b"file-bytes-for-" + asset.asset_id.encode()

    def delete(self, asset: Asset) -> None:
        if self.fail_delete:
            raise RuntimeError("delete boom")
        self.deleted.append(asset.asset_id)


# ------------------------------------------------------------------
# Destination path
# ------------------------------------------------------------------

class TestDestinationPath:
    def test_organised_by_year_and_month(self):
        asset = _scored(created=datetime(2023, 7, 9, tzinfo=timezone.utc)).asset
        dest = _destination_path(asset, Path("/mnt/storage"))
        assert dest == Path("/mnt/storage/2023/07/IMG-0001.jpg")

    def test_unique_destination_when_path_reserved(self):
        base = Path("/mnt/storage/2023/07/IMG-0001.jpg")
        reserved = {base}
        assert _unique_destination(base, reserved) == Path(
            "/mnt/storage/2023/07/IMG-0001 (1).jpg"
        )

    def test_unique_destination_increments_until_free(self):
        base = Path("/mnt/storage/2023/07/IMG-0001.jpg")
        reserved = {
            base,
            Path("/mnt/storage/2023/07/IMG-0001 (1).jpg"),
        }
        assert _unique_destination(base, reserved) == Path(
            "/mnt/storage/2023/07/IMG-0001 (2).jpg"
        )


# ------------------------------------------------------------------
# Offload filename
# ------------------------------------------------------------------

class TestOffloadFilename:
    def test_real_name_is_preserved(self):
        asset = _scored(filename="IMG_2351.JPG").asset
        assert _offload_filename(asset) == "IMG_2351.JPG"

    def test_uuid_name_is_synthesised(self):
        asset = _scored(
            asset_id="b9d9d5e4-3467-4559-a0fd-5cc7d59a242a",
            filename="b9d9d5e4-3467-4559-a0fd-5cc7d59a242a.mp4",
            created=datetime(2024, 6, 15, 14, 30, 22, tzinfo=timezone.utc),
        ).asset
        assert _offload_filename(asset) == "20240615_143022_whatsapp_b9d9d5e4.mp4"

    def test_uuid_destination_uses_synthesised_name(self):
        asset = _scored(
            asset_id="b9d9d5e4-3467-4559-a0fd-5cc7d59a242a",
            filename="b9d9d5e4-3467-4559-a0fd-5cc7d59a242a.mp4",
            created=datetime(2024, 6, 15, 14, 30, 22, tzinfo=timezone.utc),
        ).asset
        dest = _destination_path(asset, Path("/mnt/storage"))
        assert dest == Path(
            "/mnt/storage/2024/06/20240615_143022_whatsapp_b9d9d5e4.mp4"
        )


# ------------------------------------------------------------------
# Dry-run
# ------------------------------------------------------------------

class TestDryRun:
    def test_reports_would_offload_and_writes_nothing(self, tmp_path):
        results = offload([_scored()], dry_run=True, mount_path=str(tmp_path))
        assert len(results) == 1
        assert results[0].status == OffloadStatus.WOULD_OFFLOAD
        assert list(tmp_path.rglob("*.jpg")) == []

    def test_dry_run_needs_no_source(self, tmp_path):
        # Should not raise despite source=None.
        results = offload([_scored()], dry_run=True, mount_path=str(tmp_path))
        assert results[0].destination.endswith("IMG-0001.jpg")

    def test_collision_within_batch_gets_unique_paths(self, tmp_path):
        created = datetime(2023, 7, 9, tzinfo=timezone.utc)
        items = [
            _scored(asset_id="a", filename="IMG-0001.jpg", created=created),
            _scored(asset_id="b", filename="IMG-0001.jpg", created=created),
        ]
        results = offload(items, dry_run=True, mount_path=str(tmp_path))
        destinations = {r.destination for r in results}
        assert len(destinations) == 2


# ------------------------------------------------------------------
# Live offload
# ------------------------------------------------------------------

class TestLiveOffload:
    def test_writes_file_then_deletes(self, tmp_path):
        source = FakeSource()
        results = offload(
            [_scored()], dry_run=False, source=source, mount_path=str(tmp_path)
        )
        assert results[0].status == OffloadStatus.OFFLOADED
        written = tmp_path / "2023" / "07" / "IMG-0001.jpg"
        assert written.read_bytes() == b"file-bytes-for-abc123"
        assert source.deleted == ["abc123"]

    def test_missing_source_fails_without_writing(self, tmp_path):
        results = offload(
            [_scored()], dry_run=False, source=None, mount_path=str(tmp_path)
        )
        assert results[0].status == OffloadStatus.FAILED
        assert list(tmp_path.rglob("*")) == []

    def test_download_failure_does_not_delete(self, tmp_path):
        source = FakeSource(fail_download=True)
        results = offload(
            [_scored()], dry_run=False, source=source, mount_path=str(tmp_path)
        )
        assert results[0].status == OffloadStatus.FAILED
        assert source.deleted == []
        assert list(tmp_path.rglob("*.jpg")) == []

    def test_delete_failure_keeps_written_file_and_reports(self, tmp_path):
        source = FakeSource(fail_delete=True)
        results = offload(
            [_scored()], dry_run=False, source=source, mount_path=str(tmp_path)
        )
        assert results[0].status == OffloadStatus.FAILED
        assert "not deleted" in results[0].detail
        written = tmp_path / "2023" / "07" / "IMG-0001.jpg"
        assert written.exists()

    def test_live_collision_writes_both_files(self, tmp_path):
        created = datetime(2023, 7, 9, tzinfo=timezone.utc)
        source = FakeSource()
        items = [
            _scored(asset_id="a", filename="IMG-0001.jpg", created=created),
            _scored(asset_id="b", filename="IMG-0001.jpg", created=created),
        ]
        offload(items, dry_run=False, source=source, mount_path=str(tmp_path))
        files = sorted(p.name for p in (tmp_path / "2023" / "07").iterdir())
        assert files == ["IMG-0001 (1).jpg", "IMG-0001.jpg"]
