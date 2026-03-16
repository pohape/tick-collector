"""Verify WebDAV credentials and connectivity."""

import sys

import requests

import settings
from maintain import WebDAVClient

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
CHECK = f"{GREEN}\u2714{RESET}"
CROSS = f"{RED}\u2718{RESET}"


def _ok(msg: str) -> None:
    print(f"  {CHECK} {msg}")


def _fail(msg: str) -> None:
    print(f"  {CROSS} {msg}")


def main() -> int:
    print(f"\nWebDAV: {settings.WEBDAV_URL}")
    print(f"User:   {settings.WEBDAV_USER}\n")

    dav = WebDAVClient(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)

    # 1. Connect and check quota
    avail, used = dav.quota()
    if avail is None:
        _fail("Connection failed")
        return 1
    _ok("Connected")

    total = avail + (used or 0)
    _ok(f"Free space: {avail / (1024 * 1024):.0f} / {total / (1024 * 1024):.0f} MB")

    # 2. Create test directory
    test_dir = "_tick_collector_test"
    try:
        dav.ensure_dir(test_dir)
        exists, _ = dav.exists(test_dir)
        if not exists:
            _fail(f"Create directory '{test_dir}'")
            return 1
        _ok(f"Created directory '{test_dir}'")
    except requests.RequestException as e:
        _fail(f"Create directory: {e}")
        return 1

    # 3. Upload test file
    import tempfile
    from pathlib import Path

    test_remote = f"{test_dir}/test.txt"
    tmp = Path(tempfile.mktemp())
    tmp.write_text("tick-collector webdav check\n")
    try:
        if not dav.upload(tmp, test_remote):
            _fail("Upload test file")
            return 1
        exists, size = dav.exists(test_remote)
        if not exists:
            _fail("Upload verification")
            return 1
        _ok(f"Uploaded test file ({size} bytes)")
    except requests.RequestException as e:
        _fail(f"Upload: {e}")
        return 1
    finally:
        tmp.unlink(missing_ok=True)

    # 4. Delete test file
    if dav.delete(test_remote):
        _ok("Deleted test file")
    else:
        _fail("Delete test file")
        return 1

    # 5. Delete test directory
    if dav.delete(test_dir):
        _ok("Deleted test directory")
    else:
        _fail("Delete test directory")
        return 1

    print(f"\n  {CHECK} All checks passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
