"""Verify WebDAV credentials and connectivity."""

import sys
import tempfile
from pathlib import Path

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
    tmp = Path(tempfile.mktemp())
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

    _ok("All checks passed")
    return True


def _free_mb(url: str, user: str, password: str) -> int:
    dav = WebDAVClient(url, user, password)
    avail, _ = dav.quota()
    if avail is None:
        print("error: connection failed", file=sys.stderr)
        sys.exit(1)
    return int(avail / (1024 * 1024))


def _has_secondary() -> bool:
    return bool(settings.WEBDAV2_URL and settings.WEBDAV2_USER and settings.WEBDAV2_PASSWORD)


def main() -> int:
    ok1 = _check_one("primary", settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)

    ok2 = True
    if _has_secondary():
        ok2 = _check_one("secondary", settings.WEBDAV2_URL, settings.WEBDAV2_USER, settings.WEBDAV2_PASSWORD)
    print()

    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    if "--free-mb-only" in sys.argv:
        print(_free_mb(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD))
    elif "--free-mb-only2" in sys.argv:
        if not _has_secondary():
            print("error: secondary WebDAV not configured (WEBDAV2_URL, WEBDAV2_USER, WEBDAV2_PASSWORD)",
                  file=sys.stderr)
            sys.exit(1)
        print(_free_mb(settings.WEBDAV2_URL, settings.WEBDAV2_USER, settings.WEBDAV2_PASSWORD))
    else:
        sys.exit(main())
