"""Nightly maintenance: compress closed CSVs, upload to Mail.ru Cloud, clean old files."""

import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
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
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    log.info(
        "starting maintenance (today=%s, retention=%dd, cutoff=%s)",
        today,
        settings.RETENTION_DAYS,
        cutoff_str
    )

    dav = WebDAVClient(settings.MAIL_WEBDAV_URL, settings.MAIL_USER, settings.MAIL_APP_PASSWORD)
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

    # Phase 3: CLEANUP
    log.info("phase 3: cleanup (cutoff=%s)", cutoff_str)

    for zst_path in sorted(settings.DATA_DIR.rglob("*.csv.zst")):
        file_date = extract_date(zst_path.name)

        if file_date is None or file_date >= cutoff_str:
            continue

        remote = _remote_path(zst_path)
        exists, _ = dav.exists(remote)

        if not exists:
            log.warning("skipping delete, not in cloud: %s", zst_path)
            n_errors += 1

            continue

        zst_path.unlink()
        log.info("deleted local %s (date=%s, confirmed in cloud)", zst_path, file_date)
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
        n_errors
    )

    return 1 if n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
