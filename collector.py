"""Main entry point: launch stream tasks and writer, handle graceful shutdown."""

import asyncio
import logging
import signal
import time

import settings
from exchanges import binance, bybit
from writer import TickWriter

log = logging.getLogger("collector")


async def _gap_monitor(last_msg_mono: dict) -> None:
    """Periodically warn when a symbol has no updates for too long."""
    threshold = settings.GAP_WARN_SECONDS
    while True:
        await asyncio.sleep(threshold)
        now = time.monotonic()
        for key, t in list(last_msg_mono.items()):
            gap = now - t
            if gap > threshold:
                log.warning("gap: %s/%s no update for %.1fs", *key, gap)


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("starting tick collector")
    log.info("data dir: %s", settings.DATA_DIR)
    log.info("binance symbols: %s", settings.BINANCE_SYMBOLS)
    log.info("bybit symbols: %s", settings.BYBIT_SYMBOLS)

    queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
    last_state: dict = {}
    last_msg_mono: dict = {}
    reconnect_count: dict = {}

    writer = TickWriter(queue)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info("shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    tasks = [
        asyncio.create_task(binance.stream(queue, last_state, last_msg_mono, reconnect_count),
                            name="binance_stream"),
        asyncio.create_task(bybit.stream(queue, last_state, last_msg_mono, reconnect_count),
                            name="bybit_stream"),
        asyncio.create_task(writer.run(), name="writer"),
        asyncio.create_task(_gap_monitor(last_msg_mono), name="gap_monitor"),
    ]

    await shutdown_event.wait()
    log.info("shutting down: cancelling streams")

    # cancel streams and gap monitor, keep writer alive
    for t in tasks:
        if t.get_name() != "writer":
            t.cancel()

    for t in tasks:
        if t.get_name() != "writer":
            try:
                await t
            except asyncio.CancelledError:
                pass

    # signal writer to stop and wait for drain
    await queue.put(None)
    writer_task = [t for t in tasks if t.get_name() == "writer"][0]
    try:
        await asyncio.wait_for(writer_task, timeout=25)
    except asyncio.TimeoutError:
        log.warning("writer did not finish in time, cancelling")
        writer_task.cancel()
        try:
            await writer_task
        except asyncio.CancelledError:
            pass

    log.info("tick collector stopped, reconnect counts: %s", reconnect_count)


if __name__ == "__main__":
    asyncio.run(main())
