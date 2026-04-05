"""Tests for the worker execution concurrency semaphore.

Verifies that WorkerExecutor limits concurrent container creation via
an asyncio.Semaphore — not a pool. No pre-warming, no container reuse.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from worker_executor import WorkerExecutor, WorkerResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(max_workers: int = 4, **kwargs) -> WorkerExecutor:
    """Create a WorkerExecutor with a specific concurrency limit."""
    with patch.dict(os.environ, {"DRNT_MAX_CONCURRENT_WORKERS": str(max_workers)}):
        return WorkerExecutor(sandbox_base_dir="/tmp/test-workers", **kwargs)


def _dummy_call_kwargs(job_id: str = "job-1") -> dict:
    return dict(
        worker_id="w-1",
        job_id=job_id,
        capability_id="cap-1",
        image="test:latest",
        task_payload={"task_id": job_id, "task_type": "test", "payload": {}},
        resource_config={},
        security_config={},
        wall_timeout=10,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSemaphoreDefaultConcurrency:
    """Default concurrency is 4."""

    def test_default_concurrency_is_four(self):
        with patch.dict(os.environ, {}, clear=False):
            # Remove the env var if it exists
            os.environ.pop("DRNT_MAX_CONCURRENT_WORKERS", None)
            executor = WorkerExecutor(sandbox_base_dir="/tmp/test")
        assert executor._max_concurrent_workers == 4
        assert executor._semaphore._value == 4

    def test_env_var_overrides_default(self):
        executor = _make_executor(max_workers=8)
        assert executor._max_concurrent_workers == 8
        assert executor._semaphore._value == 8


class TestSemaphoreLimitsConcurrency:
    """Semaphore limits concurrent executions to the configured max."""

    @pytest.mark.asyncio
    async def test_limits_to_max(self):
        max_workers = 2
        executor = _make_executor(max_workers=max_workers)

        running = asyncio.Event()
        gate = asyncio.Event()
        concurrent_count = 0
        peak_concurrent = 0

        original_execute_sync = executor._execute_sync

        def slow_execute_sync(*args, **kwargs):
            nonlocal concurrent_count, peak_concurrent
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
            running.set()
            # Block until gate is opened — simulates a long-running container
            import threading
            evt = threading.Event()
            gate_cb = lambda: evt.set()

            # We poll since we're in a thread, not async
            while not gate.is_set():
                import time
                time.sleep(0.01)

            concurrent_count -= 1
            return WorkerResult(
                success=True, response_text="ok",
                token_count_in=0, token_count_out=0,
                latency_ms=100, model="test",
            )

        executor._execute_sync = slow_execute_sync

        # Launch max_workers + 1 tasks
        tasks = []
        for i in range(max_workers + 1):
            tasks.append(asyncio.create_task(
                executor.execute(**_dummy_call_kwargs(job_id=f"job-{i}"))
            ))

        # Let the first batch start running
        await asyncio.sleep(0.1)

        # Only max_workers should be running concurrently
        assert peak_concurrent <= max_workers

        # Release the gate so all tasks can finish
        gate.set()
        results = await asyncio.gather(*tasks)
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_waiting_jobs_eventually_execute(self):
        """Jobs that exceed the limit wait and eventually execute."""
        max_workers = 1
        executor = _make_executor(max_workers=max_workers)

        call_order = []

        def tracking_execute_sync(*args, **kwargs):
            job_id = args[1]  # second positional arg
            call_order.append(job_id)
            return WorkerResult(
                success=True, response_text=f"done-{job_id}",
                token_count_in=0, token_count_out=0,
                latency_ms=50, model="test",
            )

        executor._execute_sync = tracking_execute_sync

        results = await asyncio.gather(
            executor.execute(**_dummy_call_kwargs(job_id="job-a")),
            executor.execute(**_dummy_call_kwargs(job_id="job-b")),
            executor.execute(**_dummy_call_kwargs(job_id="job-c")),
        )

        assert len(call_order) == 3
        assert all(r.success for r in results)


class TestSemaphoreTimeout:
    """Timeout produces a clear error result."""

    @pytest.mark.asyncio
    async def test_timeout_returns_error_result(self):
        executor = _make_executor(max_workers=1)
        executor._semaphore_timeout = 0.1  # 100ms for fast test

        gate = asyncio.Event()

        def blocking_execute_sync(*args, **kwargs):
            while not gate.is_set():
                import time
                time.sleep(0.01)
            return WorkerResult(
                success=True, response_text="ok",
                token_count_in=0, token_count_out=0,
                latency_ms=100, model="test",
            )

        executor._execute_sync = blocking_execute_sync

        # First task grabs the only slot
        task1 = asyncio.create_task(
            executor.execute(**_dummy_call_kwargs(job_id="holder"))
        )
        await asyncio.sleep(0.05)  # let it acquire

        # Second task should timeout
        result = await executor.execute(**_dummy_call_kwargs(job_id="waiter"))

        assert result.success is False
        assert result.error == "max concurrent workers reached"

        # Cleanup
        gate.set()
        await task1


class TestSemaphoreRelease:
    """Semaphore is released on both success and failure paths."""

    @pytest.mark.asyncio
    async def test_released_on_success(self):
        executor = _make_executor(max_workers=1)

        def ok_sync(*args, **kwargs):
            return WorkerResult(
                success=True, response_text="ok",
                token_count_in=0, token_count_out=0,
                latency_ms=50, model="test",
            )

        executor._execute_sync = ok_sync

        await executor.execute(**_dummy_call_kwargs())
        # Semaphore should be back to 1
        assert executor._semaphore._value == 1

    @pytest.mark.asyncio
    async def test_released_on_failure(self):
        executor = _make_executor(max_workers=1)

        def failing_sync(*args, **kwargs):
            raise RuntimeError("container exploded")

        executor._execute_sync = failing_sync

        with pytest.raises(Exception, match="container exploded"):
            await executor.execute(**_dummy_call_kwargs())

        # Semaphore must still be released
        assert executor._semaphore._value == 1

    @pytest.mark.asyncio
    async def test_released_after_worker_execution_error(self):
        executor = _make_executor(max_workers=1)

        from worker_executor import WorkerExecutionError

        def raising_sync(*args, **kwargs):
            raise WorkerExecutionError("sidecar down")

        executor._execute_sync = raising_sync

        with pytest.raises(WorkerExecutionError):
            await executor.execute(**_dummy_call_kwargs())

        assert executor._semaphore._value == 1
