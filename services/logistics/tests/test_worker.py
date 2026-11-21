"""Tests for the arq logistics worker (nightly pooling cron and pool jobs)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from logistics_service.worker import WorkerSettings, nightly_pool, run_pool_job


@pytest.mark.asyncio
async def test_run_pool_job_calls_pooling_engine() -> None:
    ctx = {
        "settings": AsyncMock(order_service_url="http://order:8000"),
        "session_factory": AsyncMock(),
    }

    mock_orders = [{"order_id": "11111111-1111-1111-1111-111111111111"}]
    mock_pools = [AsyncMock(id="pool-1"), AsyncMock(id="pool-2")]

    with (
        patch(
            "logistics_service.worker.fetch_ready_orders", new=AsyncMock(return_value=mock_orders)
        ) as mock_fetch,
        patch(
            "logistics_service.worker.run_pooling", new=AsyncMock(return_value=mock_pools)
        ) as mock_run,
    ):
        count = await run_pool_job(ctx, "2026-11-18")
        assert count == 2
        mock_fetch.assert_awaited_once_with(date(2026, 11, 18), "http://order:8000")
        mock_run.assert_awaited_once_with(
            date(2026, 11, 18), mock_orders, ctx["session_factory"], ctx["settings"]
        )


@pytest.mark.asyncio
async def test_nightly_pool_triggers_for_today() -> None:
    ctx = {
        "settings": AsyncMock(order_service_url="http://order:8000"),
        "session_factory": AsyncMock(),
    }

    today = date.today()
    with patch("logistics_service.worker.run_pool_job", new=AsyncMock(return_value=3)) as mock_job:
        count = await nightly_pool(ctx)
        assert count == 3
        mock_job.assert_awaited_once_with(ctx, today.isoformat())


def test_worker_does_not_use_the_shared_default_queue() -> None:
    """The media worker runs on the same Redis. On arq's default queue each worker took the other's
    jobs, found no such function and dropped them."""
    assert WorkerSettings.queue_name not in (None, "arq:queue")
