"""Bybit V5 Linear tickers stream reader with snapshot/delta merge."""

import asyncio
import json
import logging
import ssl
import time

import websockets

import settings

log = logging.getLogger(__name__)

EXCHANGE = "bybit"
BASE_URL = "wss://stream.bybit.com/v5/public/linear"


def _subscribe_msg(symbols: list[str]) -> str:
    return json.dumps({"op": "subscribe", "args": [f"tickers.{s}" for s in symbols]})


class BybitState:
    """Maintains per-symbol top-of-book state for delta merges."""

    def __init__(self):
        self._state: dict[str, dict] = {}

    def update(self, msg: dict) -> dict | None:
        data = msg.get("data")

        if data is None:
            return None

        symbol = data.get("symbol")

        if symbol is None:
            return None

        ts = msg.get("ts")

        if ts is None:
            return None

        msg_type = msg.get("type", "")

        if msg_type == "snapshot":
            self._state[symbol] = {
                "bid": data.get("bid1Price"),
                "bid_size": data.get("bid1Size"),
                "ask": data.get("ask1Price"),
                "ask_size": data.get("ask1Size"),
            }
        elif msg_type == "delta":
            cur = self._state.get(symbol)

            if cur is None:
                return None
            if "bid1Price" in data:
                cur["bid"] = data["bid1Price"]
            if "bid1Size" in data:
                cur["bid_size"] = data["bid1Size"]
            if "ask1Price" in data:
                cur["ask"] = data["ask1Price"]
            if "ask1Size" in data:
                cur["ask_size"] = data["ask1Size"]
        else:
            return None

        cur = self._state.get(symbol)

        if cur is None or any(v is None for v in cur.values()):
            return None

        return {
            "exchange": EXCHANGE,
            "symbol": symbol,
            "ts": ts,
            "bid": cur["bid"],
            "ask": cur["ask"],
            "bid_size": cur["bid_size"],
            "ask_size": cur["ask_size"],
        }


async def stream(queue: asyncio.Queue, last_state: dict, last_msg_mono: dict, reconnect_count: dict) -> None:
    url = BASE_URL
    backoff = 3
    connect_kwargs: dict = dict(ping_interval=20, ping_timeout=10, close_timeout=5)

    if settings.WS_TEST_MODE:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        connect_kwargs["ssl"] = ssl_ctx

    while True:
        try:
            log.info("[bybit] connecting to %s", url)

            async with websockets.connect(url, **connect_kwargs) as ws:
                backoff = 3
                bybit = BybitState()

                await ws.send(_subscribe_msg(settings.BYBIT_SYMBOLS))
                log.info("[bybit] connected, subscribed to %d symbols", len(settings.BYBIT_SYMBOLS))

                ping_task = asyncio.create_task(_ping_loop(ws))

                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        # skip subscribe responses and pong

                        if "op" in msg:
                            continue

                        tick = bybit.update(msg)

                        if tick is None:
                            continue

                        key = (EXCHANGE, tick["symbol"])
                        price = (tick["bid"], tick["ask"])

                        if last_state.get(key) == price:
                            continue

                        last_state[key] = price
                        last_msg_mono[key] = time.monotonic()

                        await queue.put(tick)
                finally:
                    ping_task.cancel()

                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        except (websockets.ConnectionClosed, ConnectionError, TimeoutError, OSError) as e:
            reconnect_count[EXCHANGE] = reconnect_count.get(EXCHANGE, 0) + 1
            log.warning("[bybit] disconnected: %s; reconnect #%d in %ds", e, reconnect_count[EXCHANGE], backoff)

            await asyncio.sleep(backoff)

            backoff = min(backoff * 2, 30)
        except asyncio.CancelledError:
            log.info("[bybit] stream cancelled")

            return


async def _ping_loop(ws) -> None:
    try:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))
    except (asyncio.CancelledError, websockets.ConnectionClosed):
        pass
