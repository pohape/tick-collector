"""Binance USDM Futures bookTicker combined stream reader."""

import asyncio
import json
import logging
import ssl
import time

import websockets

import settings

log = logging.getLogger(__name__)

EXCHANGE = "binance"
BASE_URL = "wss://fstream.binance.com"


def _build_url(symbols: list[str]) -> str:
    streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
    return f"{BASE_URL}/stream?streams={streams}"


def _parse(raw: str) -> dict | None:
    msg = json.loads(raw)
    data = msg.get("data")

    if data is None:
        return None
    elif data.get("e") != "bookTicker":
        return None

    return {
        "exchange": EXCHANGE,
        "symbol": data["s"],
        "ts": data["E"],
        "bid": data["b"],
        "ask": data["a"],
        "bid_size": data["B"],
        "ask_size": data["A"],
    }


async def stream(
        queue: asyncio.Queue,
        last_state: dict,
        last_msg_mono: dict,
        reconnect_count: dict,
) -> None:
    url = _build_url(settings.BINANCE_SYMBOLS)
    backoff = 3
    connect_kwargs: dict = dict(ping_interval=20, ping_timeout=10, close_timeout=5)

    if settings.WS_TEST_MODE:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_kwargs["ssl"] = ssl_ctx

    while True:
        try:
            log.info("[binance] connecting to %s", url)

            async with websockets.connect(url, **connect_kwargs) as ws:
                backoff = 3
                log.info("[binance] connected")

                async for raw in ws:
                    tick = _parse(raw)

                    if tick is None:
                        continue

                    key = (EXCHANGE, tick["symbol"])
                    state = (tick["bid"], tick["bid_size"], tick["ask"], tick["ask_size"])

                    if last_state.get(key) == state:
                        continue

                    last_state[key] = state
                    last_msg_mono[key] = time.monotonic()
                    await queue.put(tick)
        except (websockets.ConnectionClosed, ConnectionError, TimeoutError, OSError) as e:
            reconnect_count[EXCHANGE] = reconnect_count.get(EXCHANGE, 0) + 1
            log.warning("[binance] disconnected: %s; reconnect #%d in %ds", e, reconnect_count[EXCHANGE], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except asyncio.CancelledError:
            log.info("[binance] stream cancelled")

            return
