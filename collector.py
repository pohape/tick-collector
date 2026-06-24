"""Main entry point: launch stream tasks, writer, and nightly maintenance."""

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone, timedelta

import maintain
import settings
from exchanges import binance, bybit
from writer import TickWriter

log = logging.getLogger("collector")

# Set in main(); used by _on_task_done to request a supervised restart.
_shutdown_event: "asyncio.Event | None" = None


async def _maintenance_loop() -> None:
    """Run maintenance daily at 00:01 UTC. Self-healing: exceptions
    inside maintain.main() are logged and the loop continues to the
    next day. Without this guard, a single crash would silently kill
    the asyncio task and stop nightly compress/upload indefinitely."""
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        wait = (tomorrow - now).total_seconds()
        log.info("next maintenance in %.0fs (at %s UTC)", wait, tomorrow.strftime("%Y-%m-%d %H:%M"))
        await asyncio.sleep(wait)
        log.info("starting maintenance")

        try:
            exit_code = await asyncio.to_thread(maintain.main)
            log.info("maintenance done (exit_code=%d)", exit_code)
        except Exception:
            log.exception("maintenance crashed; will retry tomorrow")


async def _gap_monitor(last_msg_mono: dict) -> None:
    """Periodically warn when a symbol has no updates for too long.
    Per-iteration try/except keeps the loop alive across unexpected
    errors (same self-healing pattern as _maintenance_loop)."""
    threshold = settings.GAP_WARN_SECONDS
    while True:
        await asyncio.sleep(threshold)

        try:
            now = time.monotonic()
            for key, t in list(last_msg_mono.items()):
                gap = now - t
                if gap > threshold:
                    log.warning("gap: %s/%s no update for %.1fs", *key, gap)
        except Exception:
            log.exception("gap_monitor iteration failed")


def _on_task_done(task: asyncio.Task) -> None:
    """Done-callback for the long-running tasks. A stream/writer/monitor is
    meant to run forever, so if one ends here it is an unrecoverable partial
    failure (e.g. one exchange silently dead). Log it and request a clean
    shutdown so the systemd supervisor (Restart=always) restarts the whole
    process fresh, instead of limping on degraded."""
    if task.cancelled():
        return

    exc = task.exception()
    if exc is not None:
        log.error("task %s died with exception", task.get_name(), exc_info=exc)
    else:
        log.error("task %s exited unexpectedly", task.get_name())

    if _shutdown_event is not None and not _shutdown_event.is_set():
        log.error("critical task ended -> shutting down for supervised restart")
        _shutdown_event.set()


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
    global _shutdown_event
    _shutdown_event = shutdown_event

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
        asyncio.create_task(_maintenance_loop(), name="maintenance"),
    ]

    for t in tasks:
        t.add_done_callback(_on_task_done)

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
