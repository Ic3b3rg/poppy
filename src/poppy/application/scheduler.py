import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from poppy.application.review_cycle import ReviewRequest

logger = logging.getLogger(__name__)


class InMemoryScheduler:
    def __init__(
        self,
        *,
        handler: Callable[[ReviewRequest], Awaitable[None]],
        concurrency: int,
        history_size: int = 1_000,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        if history_size < 1:
            raise ValueError("history size must be positive")
        self._handler = handler
        self._semaphore = asyncio.Semaphore(concurrency)
        self._history_size = history_size
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def schedule(self, request: ReviewRequest) -> bool:
        async with self._lock:
            key = request.dedupe_key
            if key in self._seen:
                return False
            self._seen[key] = None
            if len(self._seen) > self._history_size:
                self._seen.popitem(last=False)

            task = asyncio.create_task(self._execute(request))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return True

    async def join(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, request: ReviewRequest) -> None:
        async with self._semaphore:
            try:
                await self._handler(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "review cycle failed",
                    extra={
                        "repository_id": request.ref.repository_id,
                        "pull_request": request.ref.number,
                        "head_sha": request.head_sha,
                    },
                )
