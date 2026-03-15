"""Queue consumer: CSV append with UTC date rollover."""

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import settings

log = logging.getLogger(__name__)

CSV_HEADER = ["ts", "bid", "ask", "bid_size", "ask_size"]


def _date_from_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _file_path(exchange: str, symbol: str, date_str: str) -> Path:
    return settings.DATA_DIR / exchange / symbol / f"{date_str}.csv"


class TickWriter:
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self._handles: dict[tuple[str, str], io.TextIOWrapper] = {}
        self._current_dates: dict[tuple[str, str], str] = {}
        self._row_counts: dict[tuple[str, str], int] = {}

    def _open(self, exchange: str, symbol: str, date_str: str) -> io.TextIOWrapper:
        key = (exchange, symbol)
        path = _file_path(exchange, symbol, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        fh = open(path, "a", newline="")

        if is_new:
            w = csv.writer(fh)
            w.writerow(CSV_HEADER)
            fh.flush()

        self._handles[key] = fh
        self._current_dates[key] = date_str
        self._row_counts[key] = 0
        log.info("opened %s", path)

        return fh

    def _close(self, key: tuple[str, str]) -> None:
        fh = self._handles.pop(key, None)

        if fh:
            fh.flush()
            fh.close()
            log.info("closed file for %s/%s", *key)

        self._current_dates.pop(key, None)
        self._row_counts.pop(key, None)

    def _get_handle(self, exchange: str, symbol: str, date_str: str) -> io.TextIOWrapper:
        key = (exchange, symbol)
        cur_date = self._current_dates.get(key)

        if cur_date and cur_date != date_str:
            self._close(key)

        if key not in self._handles:
            return self._open(exchange, symbol, date_str)

        return self._handles[key]

    def _write_tick(self, tick: dict) -> None:
        exchange = tick["exchange"]
        symbol = tick["symbol"]
        date_str = _date_from_ms(tick["ts"])
        fh = self._get_handle(exchange, symbol, date_str)
        w = csv.writer(fh)
        w.writerow([tick["ts"], tick["bid"], tick["ask"], tick["bid_size"], tick["ask_size"]])
        key = (exchange, symbol)
        self._row_counts[key] = self._row_counts.get(key, 0) + 1

        if self._row_counts[key] >= settings.FLUSH_EVERY:
            fh.flush()
            self._row_counts[key] = 0

    def flush_all(self) -> None:
        for fh in self._handles.values():
            try:
                fh.flush()
            except OSError:
                pass

    def close_all(self) -> None:
        for key in list(self._handles):
            self._close(key)

    async def run(self) -> None:
        log.info("writer started")

        try:
            while True:
                tick = await self._queue.get()

                if tick is None:
                    break

                self._write_tick(tick)
                self._queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            # drain remaining items
            while not self._queue.empty():
                try:
                    tick = self._queue.get_nowait()

                    if tick is not None:
                        self._write_tick(tick)
                except asyncio.QueueEmpty:
                    break

            self.flush_all()
            self.close_all()
            log.info("writer stopped")
