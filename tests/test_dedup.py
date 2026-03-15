"""Test that writer writes all ticks it receives (dedup is in stream, not writer)."""

import asyncio
import csv

import settings
from writer import TickWriter

# 2026-03-15 00:00:00.000 UTC
TS_MAR15 = 1773532800000


def _tick(ts, bid="100", ask="101"):
    return {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "ts": ts,
        "bid": bid,
        "ask": ask,
        "bid_size": "10",
        "ask_size": "5",
    }


async def _run_writer(ticks):
    q = asyncio.Queue()
    w = TickWriter(q)
    for t in ticks:
        await q.put(t)
    await q.put(None)
    await w.run()


def test_writer_writes_all_ticks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FLUSH_EVERY", 1)

    ticks = [
        _tick(TS_MAR15, bid="100"),
        _tick(TS_MAR15 + 1, bid="100"),  # same bid, different ts
        _tick(TS_MAR15 + 2, bid="101"),  # different bid
    ]
    asyncio.run(_run_writer(ticks))

    csv_path = tmp_path / "binance" / "BTCUSDT" / "2026-03-15.csv"
    with open(csv_path) as f:
        rows = list(csv.reader(f))
    # header + 3 rows (writer writes all, dedup is in stream)
    assert len(rows) == 4
