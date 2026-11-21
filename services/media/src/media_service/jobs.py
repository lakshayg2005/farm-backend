"""Queueing photo processing as arq jobs on Redis (HLD section 8: arq workers in the service)."""

import logging
from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Request

logger = logging.getLogger(__name__)

PROCESS_MEDIA = "process_media"
# Own queue: arq's default queue is shared by every worker on this Redis, and another service's worker
# (logistics) would take media jobs it cannot run and drop them.
QUEUE_NAME = "media:jobs"


def job_id(media_id: UUID | str) -> str:
    # A fixed job id per photo: arq refuses a second copy while one is queued or running.
    return f"media:{media_id}"


class JobQueue(Protocol):
    async def enqueue(self, media_id: UUID) -> None: ...


class ArqQueue:
    def __init__(self, pool: ArqRedis) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, redis_url: str) -> "ArqQueue":
        return cls(await create_pool(RedisSettings.from_dsn(redis_url)))

    async def enqueue(self, media_id: UUID) -> None:
        try:
            await self.pool.enqueue_job(
                PROCESS_MEDIA, str(media_id), _job_id=job_id(media_id), _queue_name=QUEUE_NAME
            )
        except Exception:
            # The row stays PROCESSING; the worker's sweeper queues it again later.
            logger.exception("Could not queue media %s; the sweeper will retry", media_id)

    async def close(self) -> None:
        await self.pool.aclose()


class NoQueue:
    """Used when JOBS_ENABLED=false or Redis was unreachable at startup."""

    async def enqueue(self, media_id: UUID) -> None:
        logger.warning("Jobs are disabled; media %s waits for the sweeper", media_id)


def get_queue(request: Request) -> JobQueue:
    queue: JobQueue | None = getattr(request.app.state, "queue", None)
    return queue or NoQueue()
