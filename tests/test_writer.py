"""Test writer: CSV output and UTC rollover."""

import asyncio
import csv

import pytest

import settings
from writer import TickWriter

# 2026-03-15 00:00:00.000 UTC
TS_MAR15 = 1773532800000
# 2026-03-15 23:59:59.999 UTC
TS_MAR15_END = 1773619199999
# 2026-03-16 00:00:00.000 UTC
TS_MAR16 = 1773619200000


def _make_tick(exchange, symbol, ts, bid="100", ask="101", bid_size="10", ask_size="5"):
    return {
        "exchange": exchange,
        "symbol": symbol,
        "ts": ts,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
    }


async def _run_writer(ticks):
    q = asyncio.Queue()
    w = TickWriter(q)
    for t in ticks:
        await q.put(t)
    await q.put(None)
    await w.run()


@pytest.fixture(autouse=True)
def _patch_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "FLUSH_EVERY", 1)


def test_writer_creates_csv(tmp_path):
    ticks = [_make_tick("binance", "BTCUSDT", TS_MAR15)]
    asyncio.run(_run_writer(ticks))

    csv_path = tmp_path / "binance" / "BTCUSDT" / "2026-03-15.csv"
    assert csv_path.exists()
    with open(csv_path) as f:
        reader = list(csv.reader(f))
    assert reader[0] == ["ts", "bid", "ask", "bid_size", "ask_size"]
    assert reader[1] == [str(TS_MAR15), "100", "101", "10", "5"]


def test_writer_utc_rollover(tmp_path):
    ticks = [
        _make_tick("binance", "BTCUSDT", TS_MAR15_END, bid="50"),
        _make_tick("binance", "BTCUSDT", TS_MAR16, bid="51"),
    ]
    asyncio.run(_run_writer(ticks))

    day1_path = tmp_path / "binance" / "BTCUSDT" / "2026-03-15.csv"
    day2_path = tmp_path / "binance" / "BTCUSDT" / "2026-03-16.csv"
    assert day1_path.exists()
    assert day2_path.exists()

    with open(day1_path) as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2  # header + 1 row
    assert rows[1][1] == "50"

    with open(day2_path) as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2
    assert rows[1][1] == "51"


def test_writer_multiple_symbols(tmp_path):
    ticks = [
        _make_tick("binance", "BTCUSDT", TS_MAR15),
        _make_tick("bybit", "ETHUSDT", TS_MAR15),
    ]
    asyncio.run(_run_writer(ticks))

    assert (tmp_path / "binance" / "BTCUSDT" / "2026-03-15.csv").exists()
    assert (tmp_path / "bybit" / "ETHUSDT" / "2026-03-15.csv").exists()
