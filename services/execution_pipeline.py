"""Trade Execution Pipeline — the ONLY path allowed to mutate broker state.

Architecture rule (Phase 0):
    Every broker-mutating MetaTrader 5 call (order_send, position close,
    SL/TP modify) MUST be submitted through this pipeline. Nothing else in the
    codebase may call those MT5 functions via asyncio.to_thread directly.

Why:
    - The MT5 Python C-extension talks to terminal64.exe over a single IPC
      channel. Concurrent calls from multiple threads serialize at the IPC
      boundary and can surface as TRADE_RETCODE_TIMEOUT (10012) or
      TRADE_CONTEXT_BUSY (10018).
    - A single worker consuming one asyncio.Queue serializes all trade ops at
      the asyncio layer, so no threading.Lock is needed inside the calls.
    - A fixed minimum interval between operations keeps us inside broker
      anti-spam limits (typically 10-20 req/s; we default far below that).

Callers await `submit(fn, *args)` and receive the callable's result (or its
exception). The worker executes each callable via asyncio.to_thread so the
event loop never blocks on the MT5 IPC round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("execution_pipeline")


class TradeExecutionPipeline:
    """Serialized, rate-limited executor for broker-mutating operations."""

    def __init__(self, rate_limit_per_sec: float = 5.0, max_queue_size: int = 200):
        if rate_limit_per_sec <= 0:
            raise ValueError("rate_limit_per_sec must be positive")
        self.interval: float = 1.0 / rate_limit_per_sec
        self.max_queue_size: int = max_queue_size
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._last_exec: float = 0.0
        self.executed_count: int = 0

    # -- lifecycle ----------------------------------------------------------

    def _ensure_queue(self) -> asyncio.Queue:
        # asyncio.Queue() is loop-agnostic from Python 3.10+; safe to build lazily.
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        return self._queue

    async def start(self) -> None:
        """Start (or restart) the worker task. Safe to call repeatedly."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Cancel the worker. Pending futures receive RuntimeError."""
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # -- stats --------------------------------------------------------------

    @property
    def depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    @property
    def running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    # -- public API ---------------------------------------------------------

    async def submit(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        """Queue a broker-mutating callable and await its result.

        `fn` must be a plain (synchronous) callable — it will be executed in a
        worker thread via asyncio.to_thread. Backpressure applies when the
        queue is full (the caller waits for room instead of dropping trades).
        """
        queue = self._ensure_queue()
        await self.start()  # self-healing: restart worker if it died
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await queue.put((fn, args, kwargs, fut))
        if timeout is not None:
            return await asyncio.wait_for(fut, timeout=timeout)
        return await fut

    # -- worker -------------------------------------------------------------

    async def _worker_loop(self) -> None:
        queue = self._ensure_queue()
        while True:
            fn, args, kwargs, fut = await queue.get()
            try:
                # Pacing: guarantee a minimum gap between broker operations.
                now = time.monotonic()
                wait = self._last_exec + self.interval - now
                if wait > 0:
                    await asyncio.sleep(wait)

                result = await asyncio.to_thread(fn, *args, **kwargs)
                self._last_exec = time.monotonic()
                self.executed_count += 1
                if not fut.done():
                    fut.set_result(result)
            except asyncio.CancelledError:
                if not fut.done():
                    fut.set_exception(RuntimeError("Execution pipeline shutting down"))
                raise
            except Exception as exc:  # deliver the failure to the awaiting caller
                logger.warning("Pipeline op failed: %s: %s", type(exc).__name__, exc)
                if not fut.done():
                    fut.set_exception(exc)
            finally:
                queue.task_done()


_default_pipeline: Optional[TradeExecutionPipeline] = None


def get_default_pipeline() -> TradeExecutionPipeline:
    """Process-wide pipeline (one MT5 terminal = one IPC queue = one pipeline)."""
    global _default_pipeline
    if _default_pipeline is None:
        _default_pipeline = TradeExecutionPipeline(rate_limit_per_sec=5.0)
    return _default_pipeline
