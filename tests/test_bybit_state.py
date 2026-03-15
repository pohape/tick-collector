"""Test Bybit snapshot/delta state merging."""

from exchanges.bybit import BybitState


def test_snapshot():
    s = BybitState()
    msg = {
        "topic": "tickers.BTCUSDT",
        "type": "snapshot",
        "data": {
            "symbol": "BTCUSDT",
            "bid1Price": "66666.60",
            "bid1Size": "23789.165",
            "ask1Price": "66666.70",
            "ask1Size": "23775.469",
        },
        "ts": 1760325052630,
    }
    tick = s.update(msg)
    assert tick is not None
    assert tick["exchange"] == "bybit"
    assert tick["symbol"] == "BTCUSDT"
    assert tick["ts"] == 1760325052630
    assert tick["bid"] == "66666.60"
    assert tick["ask"] == "66666.70"
    assert tick["bid_size"] == "23789.165"
    assert tick["ask_size"] == "23775.469"


def test_delta_updates_partial():
    s = BybitState()
    # first snapshot
    s.update({
        "topic": "tickers.BTCUSDT",
        "type": "snapshot",
        "data": {
            "symbol": "BTCUSDT",
            "bid1Price": "100.00",
            "bid1Size": "10.0",
            "ask1Price": "101.00",
            "ask1Size": "5.0",
        },
        "ts": 1000,
    })
    # delta only changes bid
    tick = s.update({
        "topic": "tickers.BTCUSDT",
        "type": "delta",
        "data": {
            "symbol": "BTCUSDT",
            "bid1Price": "100.50",
        },
        "ts": 2000,
    })
    assert tick is not None
    assert tick["ts"] == 2000
    assert tick["bid"] == "100.50"
    assert tick["ask"] == "101.00"  # unchanged
    assert tick["bid_size"] == "10.0"  # unchanged
    assert tick["ask_size"] == "5.0"  # unchanged


def test_delta_without_snapshot_returns_none():
    s = BybitState()
    tick = s.update({
        "topic": "tickers.ETHUSDT",
        "type": "delta",
        "data": {
            "symbol": "ETHUSDT",
            "bid1Price": "3000.00",
        },
        "ts": 1000,
    })
    assert tick is None


def test_no_data():
    s = BybitState()
    assert s.update({"op": "pong"}) is None


def test_unknown_type():
    s = BybitState()
    assert s.update({
        "topic": "tickers.BTCUSDT",
        "type": "unknown",
        "data": {"symbol": "BTCUSDT"},
        "ts": 1000,
    }) is None
