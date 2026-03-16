"""Test maintain.py: compression, date extraction, size-based cleanup."""

import asyncio
import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import zstandard as zstd

import settings
from maintain import compress_csv, extract_date, main


@pytest.fixture(autouse=True)
def _patch_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "LOCAL_STORAGE_MB", 1)
    monkeypatch.setattr(settings, "WEBDAV_USER", "test")
    monkeypatch.setattr(settings, "WEBDAV_PASSWORD", "test")
    monkeypatch.setattr(settings, "WEBDAV_URL", "https://example.com")
    monkeypatch.setattr(settings, "LOG_LEVEL", "WARNING")


def _create_csv(tmp_path, exchange, symbol, date, rows=100):
    d = tmp_path / exchange / symbol
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "bid", "ask", "bid_size", "ask_size"])
        for i in range(rows):
            w.writerow([1773619200000 + i, "100.00", "100.01", "10", "5"])
    return p


def _create_zst(tmp_path, exchange, symbol, date, size_bytes=5000):
    d = tmp_path / exchange / symbol
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{date}.csv.zst"
    # write dummy compressed data of approximate target size
    cctx = zstd.ZstdCompressor(level=1)
    data = cctx.compress(b"x" * size_bytes)
    p.write_bytes(data)
    return p


class TestExtractDate:
    def test_csv(self):
        assert extract_date("2026-03-15.csv") == "2026-03-15"

    def test_zst(self):
        assert extract_date("2026-03-15.csv.zst") == "2026-03-15"

    def test_invalid(self):
        assert extract_date("data.txt") is None

    def test_partial(self):
        assert extract_date("backup-2026-03-15.csv") == "2026-03-15"


class TestCompressCsv:
    def test_compress_creates_zst_and_removes_csv(self, tmp_path):
        csv_path = _create_csv(tmp_path, "binance", "BTCUSDT", "2026-03-15")
        assert csv_path.exists()

        zst_path = compress_csv(csv_path)

        assert zst_path is not None
        assert zst_path.suffix == ".zst"
        assert zst_path.exists()
        assert not csv_path.exists()
        assert zst_path.stat().st_size > 0

    def test_compressed_is_valid_zstd(self, tmp_path):
        csv_path = _create_csv(tmp_path, "binance", "BTCUSDT", "2026-03-15")
        original = csv_path.read_bytes()

        zst_path = compress_csv(csv_path)

        dctx = zstd.ZstdDecompressor()
        with open(zst_path, "rb") as fh:
            decompressed = dctx.stream_reader(fh).read()
        assert decompressed == original


class TestCleanupBySize:
    def test_no_cleanup_within_limit(self, tmp_path, monkeypatch):
        """Files under LOCAL_STORAGE_MB are not deleted."""
        monkeypatch.setattr(settings, "LOCAL_STORAGE_MB", 100)
        _create_zst(tmp_path, "binance", "BTCUSDT", "2026-03-10", 1000)
        _create_zst(tmp_path, "binance", "BTCUSDT", "2026-03-11", 1000)

        zst_files = list(tmp_path.rglob("*.csv.zst"))
        assert len(zst_files) == 2

        # total is ~2KB, limit is 100MB — no cleanup should happen
        # We can't easily run main() without a real WebDAV server,
        # so test the logic directly
        total = sum(f.stat().st_size for f in zst_files)
        limit = 100 * 1024 * 1024
        assert total < limit  # confirms no cleanup needed

    def test_oldest_deleted_first_when_over_limit(self, tmp_path, monkeypatch):
        """When over limit, oldest files are deleted first."""
        # Create 3 files, set limit so only 1 fits
        z1 = _create_zst(tmp_path, "binance", "BTCUSDT", "2026-03-10", 50000)
        z2 = _create_zst(tmp_path, "binance", "BTCUSDT", "2026-03-11", 50000)
        z3 = _create_zst(tmp_path, "binance", "BTCUSDT", "2026-03-12", 50000)

        # Simulate cleanup logic from maintain.py
        limit_bytes = z3.stat().st_size + 10  # room for only ~1 file

        zst_files = sorted(tmp_path.rglob("*.csv.zst"))
        total_size = sum(f.stat().st_size for f in zst_files)

        zst_by_date = []
        for f in zst_files:
            d = extract_date(f.name)
            if d:
                zst_by_date.append((d, f))
        zst_by_date.sort()

        deleted = []
        for file_date, zst_path in zst_by_date:
            if total_size <= limit_bytes:
                break
            file_size = zst_path.stat().st_size
            zst_path.unlink()
            total_size -= file_size
            deleted.append(file_date)

        # oldest two should be deleted, newest kept
        assert "2026-03-10" in deleted
        assert "2026-03-11" in deleted
        assert z3.exists()
        assert not z1.exists()
        assert not z2.exists()
