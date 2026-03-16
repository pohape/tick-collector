"""Integration test: full maintain.py cycle against real WebDAV server.

Uses credentials from .env. Creates a temporary cloud folder, runs all
three phases (compress, upload, cleanup), verifies results, then deletes
the test folder from the cloud.

Run with: pytest tests/test_integration.py -v -s
"""

import csv
import hashlib
import uuid
from pathlib import Path

import pytest

import settings
import maintain
from maintain import WebDAVClient, main


def _can_reach_webdav() -> bool:
    try:
        dav = WebDAVClient(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)
        avail, _ = dav.quota()
        return avail is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _can_reach_webdav(),
    reason="WebDAV server not reachable or credentials not configured",
)


def _create_csv(base: Path, exchange: str, symbol: str, date: str, rows: int = 50) -> Path:
    d = base / exchange / symbol
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "bid", "ask", "bid_size", "ask_size"])
        # use hash-based values for realistic entropy (prevents extreme compression)
        seed = f"{exchange}{symbol}{date}"
        for i in range(rows):
            h = int(hashlib.md5(f"{seed}{i}".encode()).hexdigest()[:8], 16)
            bid = 70000 + (h % 500000) / 100
            ask = bid + 0.10
            bs = (h % 99999) / 1000
            asz = ((h >> 16) % 99999) / 1000
            w.writerow([1773619200000 + i, f"{bid:.2f}", f"{ask:.2f}", f"{bs:.3f}", f"{asz:.3f}"])
    return p


@pytest.fixture()
def cloud_test_root():
    """Create a unique test folder in the cloud, yield its name, then delete it."""
    test_root = f"_test_{uuid.uuid4().hex[:8]}"
    dav = WebDAVClient(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)
    dav.ensure_dir(test_root)
    yield test_root, dav
    # cleanup: delete the entire test folder from cloud
    dav.delete(test_root)


def test_full_cycle(tmp_path, monkeypatch, cloud_test_root):
    """Test compress -> upload -> idempotency -> size-based cleanup against real WebDAV."""
    test_root, dav = cloud_test_root

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "LOG_LEVEL", "INFO")
    monkeypatch.setattr(maintain, "CLOUD_ROOT", test_root)

    # --- Setup: create 5 CSV files across old dates ---
    # Each ~25K rows -> ~1.2 MB raw -> ~250-400 KB compressed
    # Total compressed ~1.5 MB, exceeding 1 MB limit
    dates = ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"]
    for date in dates:
        _create_csv(tmp_path, "binance", "BTCUSDT", date, rows=25000)

    assert len(list(tmp_path.rglob("*.csv"))) == 5

    # --- Run 1: compress + upload (large limit, no cleanup) ---
    monkeypatch.setattr(settings, "LOCAL_STORAGE_MB", 100)
    assert main() == 0

    # All CSV compressed, originals removed
    assert len(list(tmp_path.rglob("*.csv"))) == 0
    zst_files = sorted(tmp_path.rglob("*.csv.zst"))
    assert len(zst_files) == 5

    # Verify each file < 1 MB and total > 1 MB
    sizes = {f.name: f.stat().st_size for f in zst_files}
    total = sum(sizes.values())
    for name, size in sizes.items():
        assert size < 1024 * 1024, f"{name} is {size} bytes, expected < 1 MB"
    assert total > 1024 * 1024, f"total {total} bytes, expected > 1 MB"

    # All uploaded to cloud
    for zst in zst_files:
        remote = f"{test_root}/{zst.relative_to(tmp_path)}"
        exists, remote_size = dav.exists(remote)
        assert exists, f"not in cloud: {remote}"
        assert remote_size == zst.stat().st_size

    # --- Run 2: idempotency ---
    assert main() == 0
    assert len(list(tmp_path.rglob("*.csv.zst"))) == 5

    # --- Run 3: cleanup with LOCAL_STORAGE_MB=1 ---
    monkeypatch.setattr(settings, "LOCAL_STORAGE_MB", 1)
    assert main() == 0

    remaining = sorted(tmp_path.rglob("*.csv.zst"))
    remaining_size = sum(f.stat().st_size for f in remaining)

    # Some files remain (not all deleted)
    assert len(remaining) > 0, "should keep some files locally"
    # But fewer than before (some were deleted)
    assert len(remaining) < 5, "should have deleted some files"
    # Total fits within 1 MB
    assert remaining_size <= 1024 * 1024, f"remaining {remaining_size} bytes, expected <= 1 MB"

    # Remaining files are the newest ones
    remaining_dates = sorted(maintain.extract_date(f.name) for f in remaining)
    assert remaining_dates[-1] == "2025-01-05", "newest file should survive"
    assert "2025-01-01" not in remaining_dates, "oldest should be deleted first"

    # Deleted files still exist in cloud
    for date in dates:
        remote = f"{test_root}/binance/BTCUSDT/{date}.csv.zst"
        exists, _ = dav.exists(remote)
        assert exists, f"should still be in cloud: {remote}"
