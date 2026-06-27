"""Test collector.py: _maintenance_loop self-healing and task done-callback."""

import asyncio
import logging
import time
from unittest.mock import patch

import pytest

import collector


class TestMaintenanceLoopSelfHealing:
    """Regression coverage for the 2026-04-14 outage: when maintain.main()
    raises, the asyncio task must continue running so the next day's
    maintenance still fires."""

    @pytest.mark.asyncio
    async def test_loop_continues_after_maintain_raises(self, monkeypatch, caplog):
        call_count = {"n": 0}

        def _flaky_main():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated PROPFIND crash")
            return 0

        # Capture the real sleep before patching so we can keep test-side
        # `asyncio.sleep(0)` working (otherwise the patch is reentrant).
        real_sleep = asyncio.sleep

        async def _fast_sleep(secs):
            if secs > 0.1:
                return None  # skip the loop's day-long wait
            return await real_sleep(secs)

        monkeypatch.setattr(collector, "maintain",
                            type("M", (), {"main": staticmethod(_flaky_main)}))
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        caplog.set_level(logging.INFO, logger="collector")

        task = asyncio.create_task(collector._maintenance_loop())

        # Wait until the loop has crashed once and recovered (2nd call),
        # bounded by a wall-clock deadline. A fixed spin-count is flaky:
        # each maintain.main() runs in a real worker thread via
        # asyncio.to_thread, so the number of event-loop yields needed is
        # not deterministic.
        deadline = time.monotonic() + 5.0
        while call_count["n"] < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Both calls happened — the second proves the loop survived the first crash.
        assert call_count["n"] >= 2
        # Crash was logged (not swallowed).
        assert any("maintenance crashed" in r.message for r in caplog.records)


class TestOnTaskDone:
    """The done-callback must surface task exceptions to the log."""

    @pytest.mark.asyncio
    async def test_logs_exception(self, caplog):
        async def _bad_task():
            raise ValueError("boom")

        caplog.set_level(logging.ERROR, logger="collector")

        task = asyncio.create_task(_bad_task(), name="bad")
        task.add_done_callback(collector._on_task_done)
        try:
            await task
        except ValueError:
            pass

        # add_done_callback fires from the loop after task completion.
        await asyncio.sleep(0)

        records = [r for r in caplog.records if "bad" in r.message]
        assert records, "expected error log mentioning task name"
        assert records[0].exc_info is not None
        assert records[0].exc_info[0] is ValueError

    @pytest.mark.asyncio
    async def test_silent_on_cancellation(self, caplog):
        async def _slow_task():
            await asyncio.sleep(10)

        caplog.set_level(logging.ERROR, logger="collector")

        task = asyncio.create_task(_slow_task(), name="slow")
        task.add_done_callback(collector._on_task_done)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        await asyncio.sleep(0)

        # Cancellation is not an error — must not log.
        assert not any("slow" in r.message for r in caplog.records)
