"""Nightly maintenance: compress closed CSVs, upload to Mail.ru Cloud, clean old files."""

import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import zstandard as zstd

import settings

log = logging.getLogger("maintain")

ZSTD_LEVEL = 19
UPLOAD_TIMEOUT = 300
UPLOAD_RETRIES = 3
CLOUD_ROOT = "tick-data"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.csv(\.zst)?$")


def extract_date(filename: str) -> str | None:
    m = DATE_RE.search(filename)

    return m.group(1) if m else None


class WebDAVClient:
    def __init__(self, base_url: str, user: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._created_dirs: set[str] = set()

    def _url(self, remote_path: str) -> str:
        return f"{self._base_url}/{remote_path}"

    def ensure_dir(self, remote_path: str) -> None:
        parts = remote_path.strip("/").split("/")

        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])

            if d in self._created_dirs:
                continue

            resp = self._session.request("MKCOL", self._url(d))

            if resp.status_code in (201, 405, 301):
                self._created_dirs.add(d)
            else:
                log.warning("MKCOL %s -> %d %s", d, resp.status_code, resp.text[:200])

    def exists(self, remote_path: str) -> tuple[bool, int | None]:
        resp = self._session.request(
            "PROPFIND", self._url(remote_path),
            headers={"Depth": "0", "Content-Type": "application/xml"},
        )

        if resp.status_code == 404:
            return False, None
        elif resp.status_code not in (207, 200):
            log.warning("PROPFIND %s -> %d", remote_path, resp.status_code)

            return False, None

        try:
            root = ET.fromstring(resp.text)
            ns = {"d": "DAV:"}
            el = root.find(".//d:getcontentlength", ns)
            size = int(el.text) if el is not None and el.text else None

            return True, size
        except ET.ParseError:
            return True, None

    def quota(self) -> tuple[int | None, int | None]:
        """Return (available_bytes, used_bytes) or (None, None) on error."""
        body = ('<?xml version="1.0"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop>'
                '<d:quota-available-bytes/><d:quota-used-bytes/>'
                '</d:prop></d:propfind>')
        try:
            resp = self._session.request(
                "PROPFIND", self._base_url + "/",
                headers={"Depth": "0", "Content-Type": "application/xml"},
                data=body,
            )
            
            if resp.status_code not in (207, 200):
                return None, None

            root = ET.fromstring(resp.text)
            ns = {"d": "DAV:"}
            avail_el = root.find(".//d:quota-available-bytes", ns)
            used_el = root.find(".//d:quota-used-bytes", ns)
            avail = int(avail_el.text) if avail_el is not None and avail_el.text else None
            used = int(used_el.text) if used_el is not None and used_el.text else None

            return avail, used
        except (requests.RequestException, ET.ParseError):
            return None, None

    def delete(self, remote_path: str) -> bool:
        resp = self._session.request("DELETE", self._url(remote_path))
        return resp.status_code in (200, 204, 404)

    def upload(self, local_path: Path, remote_path: str) -> bool:
        parent = "/".join(remote_path.strip("/").split("/")[:-1])

        if parent:
            self.ensure_dir(parent)

        for attempt in range(1, UPLOAD_RETRIES + 1):
            try:
                with open(local_path, "rb") as fh:
                    resp = self._session.put(
                        self._url(remote_path),
                        data=fh,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=UPLOAD_TIMEOUT,
                    )

                if resp.status_code in (200, 201, 204):
                    return True

                log.warning("PUT %s -> %d (attempt %d/%d)", remote_path, resp.status_code, attempt, UPLOAD_RETRIES)
            except requests.RequestException as e:
                log.warning("PUT %s error: %s (attempt %d/%d)", remote_path, e, attempt, UPLOAD_RETRIES)
            if attempt < UPLOAD_RETRIES:
                time.sleep(2 ** attempt)

        return False


def compress_csv(csv_path: Path) -> Path | None:
    zst_path = csv_path.with_suffix(".csv.zst")
    tmp_path = csv_path.with_suffix(".csv.zst.tmp")

    try:
        cctx = zstd.ZstdCompressor(level=ZSTD_LEVEL)

        with open(csv_path, "rb") as ifh, open(tmp_path, "wb") as ofh:
            cctx.copy_stream(ifh, ofh)

        if tmp_path.stat().st_size == 0:
            log.error("compression produced empty file: %s", csv_path)
            tmp_path.unlink(missing_ok=True)

            return None

        tmp_path.rename(zst_path)
        orig_size = csv_path.stat().st_size
        comp_size = zst_path.stat().st_size
        ratio = orig_size / comp_size if comp_size else 0
        csv_path.unlink()
        log.info("compressed %s (%d -> %d, %.1fx)", csv_path.name, orig_size, comp_size, ratio)

        return zst_path
    except Exception as e:
        log.error("compression failed %s: %s", csv_path, e)
        tmp_path.unlink(missing_ok=True)

        return None


def _remote_path(local_path: Path) -> str:
    rel = local_path.relative_to(settings.DATA_DIR)

    return f"{CLOUD_ROOT}/{rel}"


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    limit_bytes = settings.LOCAL_STORAGE_MB * 1024 * 1024

    log.info(
        "starting maintenance (today=%s, local_storage_limit=%d MB)",
        today,
        settings.LOCAL_STORAGE_MB,
    )

    dav = WebDAVClient(settings.WEBDAV_URL, settings.WEBDAV_USER, settings.WEBDAV_PASSWORD)
    n_compressed = n_uploaded = n_deleted = n_errors = 0

    # Phase 1: COMPRESS
    log.info("phase 1: compress")

    for csv_path in sorted(settings.DATA_DIR.rglob("*.csv")):
        file_date = extract_date(csv_path.name)

        if file_date is None:
            continue
        elif file_date == today:
            continue

        zst_path = csv_path.with_suffix(".csv.zst")

        if zst_path.exists():
            csv_path.unlink()
            log.info("removed leftover csv (zst exists): %s", csv_path)

            continue
        elif compress_csv(csv_path):
            n_compressed += 1
        else:
            n_errors += 1

    # Phase 2: UPLOAD
    log.info("phase 2: upload")

    for zst_path in sorted(settings.DATA_DIR.rglob("*.csv.zst")):
        remote = _remote_path(zst_path)
        local_size = zst_path.stat().st_size
        exists, remote_size = dav.exists(remote)

        if exists and remote_size == local_size:
            log.debug("already uploaded: %s", remote)

            continue
        elif dav.upload(zst_path, remote):
            ok, verified_size = dav.exists(remote)

            if ok and verified_size == local_size:
                log.info("uploaded %s (%d bytes)", remote, local_size)
                n_uploaded += 1
            else:
                log.error(
                    "upload verification failed: %s (local=%d, remote=%s)",
                    remote,
                    local_size,
                    verified_size
                )

                n_errors += 1
        else:
            log.error("upload failed: %s", remote)
            n_errors += 1

    # Phase 3: CLEANUP (delete oldest .zst files until total size fits within limit)
    zst_files = sorted(settings.DATA_DIR.rglob("*.csv.zst"))
    total_size = sum(f.stat().st_size for f in zst_files)
    log.info("phase 3: cleanup (total=%d MB, limit=%d MB)", total_size // (1024 * 1024), settings.LOCAL_STORAGE_MB)

    # sort oldest first (by date in filename)
    zst_by_date = []

    for f in zst_files:
        d = extract_date(f.name)

        if d:
            zst_by_date.append((d, f))

    zst_by_date.sort()

    for file_date, zst_path in zst_by_date:
        if total_size <= limit_bytes:
            break

        remote = _remote_path(zst_path)
        exists, _ = dav.exists(remote)

        if not exists:
            log.warning("skipping delete, not in cloud: %s", zst_path)
            n_errors += 1

            continue

        file_size = zst_path.stat().st_size
        zst_path.unlink()
        total_size -= file_size
        log.info("deleted local %s (date=%s, freed %d KB, confirmed in cloud)", zst_path, file_date, file_size // 1024)
        n_deleted += 1

    # remove empty dirs
    for d in sorted(settings.DATA_DIR.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            log.info("removed empty dir: %s", d)

    log.info(
        "done: %d compressed, %d uploaded, %d deleted, %d errors",
        n_compressed,
        n_uploaded,
        n_deleted,
        n_errors,
    )

    # Cloud quota report
    avail, used = dav.quota()

    if avail is not None and used is not None:
        avail_mb = avail / (1024 * 1024)
        used_mb = used / (1024 * 1024)
        total_mb = avail_mb + used_mb
        # estimate daily usage from today's uploaded bytes
        up_bytes = sum(f.stat().st_size for f in settings.DATA_DIR.rglob("*.csv.zst") if extract_date(f.name) == today)

        if up_bytes > 0:
            days_left = avail / up_bytes
            log.info(
                "cloud: %.0f/%.0f MB used, %.0f MB free (~%d days remaining)",
                used_mb,
                total_mb,
                avail_mb,
                int(days_left)
            )
        else:
            log.info("cloud: %.0f/%.0f MB used, %.0f MB free", used_mb, total_mb, avail_mb)

    return 1 if n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
