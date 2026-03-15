"""Test Binance message parsing."""

import json
from exchanges.binance import _parse


def test_parse_bookticker():
    msg = json.dumps({
        "stream": "bnbusdt@bookTicker",
        "data": {
            "e": "bookTicker",
            "u": 400900217,
            "E": 1568014460893,
            "T": 1568014460891,
            "s": "BNBUSDT",
            "b": "25.35190000",
            "B": "31.21000000",
            "a": "25.36520000",
            "A": "40.66000000",
        },
    })
    tick = _parse(msg)
    assert tick is not None
    assert tick["exchange"] == "binance"
    assert tick["symbol"] == "BNBUSDT"
    assert tick["ts"] == 1568014460893  # E, not T
    assert tick["bid"] == "25.35190000"
    assert tick["ask"] == "25.36520000"
    assert tick["bid_size"] == "31.21000000"
    assert tick["ask_size"] == "40.66000000"


def test_parse_no_data():
    msg = json.dumps({"result": None, "id": 1})
    assert _parse(msg) is None


def test_parse_wrong_event_type():
    msg = json.dumps({
        "stream": "bnbusdt@aggTrade",
        "data": {
            "e": "aggTrade",
            "E": 1568014460893,
            "s": "BNBUSDT",
        },
    })
    assert _parse(msg) is None
