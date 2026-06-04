"""Async job manager. Submitting keys returns a job_id immediately; per-key
checks run concurrently under a user-configurable semaphore. Progress for each
key streams to subscribers (SSE) via an asyncio.Queue fan-out.

In-memory only — fine for a single-process tool. Completed jobs are evicted
after settings.job_ttl_seconds.
"""

from __future__ import annotations

import asyncio
import secrets
import time

from app.config import settings
from app.http_util import make_client
from app.models import (
    CheckMode,
    JobRequest,
    JobSnapshot,
    KeyResult,
    KeyStatus,
)
from app.plugins.base import CheckContext, mask_key, redact
from app.plugins.registry import dispatch


class Job:
    def __init__(self, req: JobRequest, keys: list[str]) -> None:
        self.id = secrets.token_urlsafe(9)
        self.mode = req.mode
        self.concurrency = max(1, min(req.concurrency, settings.max_concurrency))
        self.full_load = req.full_load
        self._keys = keys  # raw keys, kept in memory only, never serialized out
        self.results: list[KeyResult] = [
            KeyResult(index=i, masked_key=mask_key(k), mode=req.mode)
            for i, k in enumerate(keys)
        ]
        self.state: str = "queued"
        self.completed = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()

    # --- pub/sub for SSE ---------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _emit(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def _emit_result(self, r: KeyResult) -> None:
        self._emit({"type": "key", "data": r.model_dump(mode="json")})

    def _emit_job(self) -> None:
        self._emit({"type": "job", "data": self.summary().model_dump(mode="json")})

    # --- snapshots ---------------------------------------------------------
    def summary(self) -> JobSnapshot:
        return JobSnapshot(
            job_id=self.id,
            mode=self.mode,
            concurrency=self.concurrency,
            full_load=self.full_load,
            total=len(self.results),
            state=self.state,  # type: ignore[arg-type]
            completed=self.completed,
            started_at=self.started_at,
            finished_at=self.finished_at,
            results=self.results,
        )

    # --- execution ---------------------------------------------------------
    async def run(self) -> None:
        self.state = "running"
        self.started_at = time.time()
        self._emit_job()
        sem = asyncio.Semaphore(self.concurrency)

        async with make_client(timeout_s=60.0) as client:

            async def check_one(i: int, raw_key: str) -> None:
                async with sem:
                    if self._cancel.is_set():
                        return
                    await self._check_key(i, raw_key, client)

            await asyncio.gather(
                *(check_one(i, k) for i, k in enumerate(self._keys)),
                return_exceptions=True,
            )

        self.state = "cancelled" if self._cancel.is_set() else "done"
        self.finished_at = time.time()
        self._emit_job()
        self._emit({"type": "done", "data": {"job_id": self.id}})

    async def _check_key(self, i: int, raw_key: str, client) -> None:
        result = self.results[i]
        result.status = KeyStatus.RUNNING
        self._emit_result(result)

        plugin = dispatch(raw_key)
        if plugin is None:
            result.status = KeyStatus.UNSUPPORTED
            result.error = "no plugin matched this key format"
            self.completed += 1
            self._emit_result(result)
            self._emit_job()
            return

        result.provider = plugin.name

        async def progress(frac: float, label: str | None) -> None:
            result.progress = round(max(0.0, min(1.0, frac)), 3)
            if label is not None:
                result.progress_label = label
            self._emit_result(result)

        ctx = CheckContext(
            client=client,
            settings=settings,
            mode=self.mode,
            full_load=self.full_load,
            progress=progress,
        )

        try:
            if self.mode == CheckMode.HEALTH:
                await plugin.health_check(raw_key, result, ctx)
            else:
                await plugin.grade_check(raw_key, result, ctx)
            result.progress = 1.0
        except Exception as exc:  # genuine plugin bug
            result.status = KeyStatus.ERROR
            result.error = redact(f"{type(exc).__name__}: {exc}")
        finally:
            self.completed += 1
            self._emit_result(result)
            self._emit_job()

    def cancel(self) -> None:
        self._cancel.set()


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def _evict_stale(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.finished_at and now - j.finished_at > settings.job_ttl_seconds
        ]
        for jid in stale:
            self._jobs.pop(jid, None)

    def create(self, req: JobRequest, keys: list[str]) -> Job:
        self._evict_stale()
        job = Job(req, keys)
        self._jobs[job.id] = job
        job._task = asyncio.create_task(job.run())
        return job

    def get(self, job_id: str) -> Job | None:
        # Evict here too so memory is reclaimed even when no new jobs arrive
        # (status polling keeps calling get()), not only on create().
        self._evict_stale()
        return self._jobs.get(job_id)


manager = JobManager()
