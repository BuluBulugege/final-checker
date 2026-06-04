"""FastAPI application: submit jobs, poll/stream results, list plugins, serve UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.jobs import manager
from app.models import JobRequest, JobSnapshot
from app.plugins.registry import all_plugins

app = FastAPI(title="final-checker", description="Pluggable API key checker")

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/api/plugins")
async def list_plugins() -> dict:
    return {"plugins": [p.name for p in all_plugins()]}


@app.get("/api/config")
async def get_config() -> dict:
    """Expose tunable knobs the UI may want to show (read-only)."""
    return {
        "max_concurrency": settings.max_concurrency,
        "default_concurrency": settings.default_concurrency,
        "max_keys_per_job": settings.max_keys_per_job,
        "gemini": settings.gemini.model_dump(),
        "openai": {
            k: v
            for k, v in settings.openai.model_dump().items()
            if k not in {"rpm_buckets"}
        },
        "anthropic": settings.anthropic.model_dump(),
    }


def _parse_keys(raw: str) -> list[str]:
    keys = [line.strip() for line in raw.splitlines()]
    keys = [k for k in keys if k]
    if not keys:
        raise HTTPException(400, "no keys provided")
    if len(keys) > settings.max_keys_per_job:
        raise HTTPException(400, f"too many keys (max {settings.max_keys_per_job})")
    return keys


@app.post("/api/jobs", response_model=JobSnapshot)
async def create_job(req: JobRequest) -> JobSnapshot:
    keys = _parse_keys(req.keys)
    job = manager.create(req, keys)
    return job.summary()


@app.get("/api/jobs/{job_id}", response_model=JobSnapshot)
async def get_job(job_id: str) -> JobSnapshot:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.summary()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    job.cancel()
    return {"ok": True, "state": job.state}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")

    async def event_gen():
        # send initial full snapshot
        yield _sse("snapshot", job.summary().model_dump(mode="json"))
        q = job.subscribe()
        done_sent = False
        try:
            while True:
                # If the job already finished (possibly before we subscribed) and
                # the queue is drained, emit exactly one done and stop.
                if not done_sent and job.state in {"done", "cancelled"} and q.empty():
                    yield _sse("done", {"job_id": job.id})
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield _sse(event["type"], event["data"])
                    if event["type"] == "done":
                        done_sent = True
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment to keep connection open
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --- static UI -------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
