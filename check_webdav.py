"""Verify WebDAV credentials and connectivity."""

import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import settings
from maintain import CLOUD_ROOT, WebDAVClient

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
CHECK = f"{GREEN}\u2714{RESET}"
CROSS = f"{RED}\u2718{RESET}"

# Commodity (metals/energy) perps track underlying markets that close on
# weekends, so their top-of-book price barely moves and buffered dedup writes
# little or nothing. On Sat/Sun (UTC) we therefore don't require their files.
# Crypto (24/7) and weekday commodity files are still checked strictly, so a
# genuinely dead stream on a trading day still trips the check.
COMMODITY_SYMBOLS = {"XAUUSDT", "XAGUSDT", "CLUSDT"}
MIN_SYNCED_BYTES = 1024


def _data_synced(dav, exchange: str, symbol: str, date_str: str, is_weekend: bool) -> bool:
    if symbol in COMMODITY_SYMBOLS and is_weekend:
        return True

    exists, size = dav.exists(f"{CLOUD_ROOT}/{exchange}/{symbol}/{date_str}.csv.zst")

    return exists and bool(size) and size >= MIN_SYNCED_BYTES


def _ok(msg: str) -> None:
    print(f"  {CHECK} {msg}")


def _fail(msg: str) -> None:
    print(f"  {CROSS} {msg}")


def _check_one(label: str, url: str, user: str, password: str) -> bool:
    print(f"\n[{label}] {url}")
    print(f"  User: {user}\n")
    dav = WebDAVClient(url, user, password)
    avail, used = dav.quota()

    if avail is None:
        _fail("Connection failed")

        return False

    _ok("Connected")
    total = avail + (used or 0)
    _ok(f"Free space: {avail / (1024 * 1024):.0f} / {total / (1024 * 1024):.0f} MB")
    test_dir = "_tick_collector_test"

    try:
        dav.ensure_dir(test_dir)
        exists, _ = dav.exists(test_dir)

        if not exists:
            _fail(f"Create directory '{test_dir}'")

            return False
        _ok(f"Created directory '{test_dir}'")
    except requests.RequestException as e:
        _fail(f"Create directory: {e}")

        return False

    test_remote = f"{test_dir}/test.txt"
    tmp = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name)
    tmp.write_text("tick-collector webdav check\n")

    try:
        if not dav.upload(tmp, test_remote):
            _fail("Upload test file")

            return False
        exists, size = dav.exists(test_remote)
        if not exists:
            _fail("Upload verification")

            return False
        _ok(f"Uploaded test file ({size} bytes)")
    except requests.RequestException as e:
        _fail(f"Upload: {e}")

        return False
    finally:
        tmp.unlink(missing_ok=True)

    if dav.delete(test_remote):
        _ok("Deleted test file")
    else:
        _fail("Delete test file")

        return False

    if dav.delete(test_dir):
        _ok("Deleted test directory")
    else:
        _fail("Delete test directory")

        return False

    # Sync check: verify yesterday's data exists in cloud
    yday = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday = yday.strftime("%Y-%m-%d")
    is_weekend = yday.weekday() >= 5

    pairs = [(exchange, symbol)
             for symbols, exchange in [(settings.BINANCE_SYMBOLS, "binance"), (settings.BYBIT_SYMBOLS, "bybit")]
             for symbol in symbols]
    missing = [f"{CLOUD_ROOT}/{exchange}/{symbol}/{yesterday}.csv.zst"
               for exchange, symbol in pairs
               if not _data_synced(dav, exchange, symbol, yesterday, is_weekend)]

    if not missing:
        _ok(f"Synced: all {len(pairs)} files for {yesterday}")
    else:
        _fail(f"Missing {len(missing)}/{len(pairs)} files for {yesterday}:")

        for m in missing:
            print(f"         {m}")

        return False

    _ok("All checks passed")

    return True


def _free_mb(url: str, user: str, password: str) -> int:
    dav = WebDAVClient(url, user, password)
    avail, _ = dav.quota()

    if avail is None:
        print("error: connection failed", file=sys.stderr)
        sys.exit(1)

    return int(avail / (1024 * 1024))


def _is_synced(url: str, user: str, password: str) -> bool:
    yday = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday = yday.strftime("%Y-%m-%d")
    is_weekend = yday.weekday() >= 5
    dav = WebDAVClient(url, user, password)

    for symbols, exchange in [(settings.BINANCE_SYMBOLS, "binance"), (settings.BYBIT_SYMBOLS, "bybit")]:
        for symbol in symbols:
            if not _data_synced(dav, exchange, symbol, yesterday, is_weekend):
                return False
    return True


def _has_secondary() -> bool:
    return bool(settings.WEBDAV2_URL and settings.WEBDAV2_USER and settings.WEBDAV2_PASSWORD)


def main() -> int:
    ok1 = _check_one("primary", settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)
    ok2 = True

    if _has_secondary():
        ok2 = _check_one("secondary", settings.WEBDAV2_URL, settings.WEBDAV2_USER, settings.WEBDAV2_PASSWORD)
    print()

    return 0 if (ok1 and ok2) else 1


def _require_secondary():
    if not _has_secondary():
        print("error: secondary WebDAV not configured (WEBDAV2_URL, WEBDAV2_USER, WEBDAV2_PASSWORD)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "--free-mb-only" in sys.argv:
        print(_free_mb(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD))
    elif "--free-mb-only2" in sys.argv:
        _require_secondary()
        print(_free_mb(settings.WEBDAV2_URL, settings.WEBDAV2_USER, settings.WEBDAV2_PASSWORD))
    elif "--synced" in sys.argv:
        print(_is_synced(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD))
    elif "--synced2" in sys.argv:
        _require_secondary()
        print(_is_synced(settings.WEBDAV2_URL, settings.WEBDAV2_USER, settings.WEBDAV2_PASSWORD))
    else:
        sys.exit(main())
