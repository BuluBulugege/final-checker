"""FastAPI application: submit jobs, poll/stream results, list plugins, serve UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.api_long_term import router as long_term_router
from app.config import settings
from app.jobs import manager
from app.models import JobRequest, JobSnapshot
from app.parsing import parse_credentials
from app.plugins.registry import all_plugins
from app.scheduler import start_scheduler, stop_scheduler

app = FastAPI(title="final-checker", description="Pluggable API key checker")

# Mount long-term key management API
app.include_router(long_term_router)


@app.on_event("startup")
async def startup_event():
    """Initialize background scheduler on application startup."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Starting up final-checker application...")
    # Initialize database
    from app.db import init_db
    init_db()
    # Start background scheduler for long-term key monitoring
    await start_scheduler()
    logger.info("Application startup complete")


@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully stop background scheduler on shutdown."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Shutting down final-checker application...")
    await stop_scheduler()
    logger.info("Application shutdown complete")

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
        "max_input_chars": settings.max_input_chars,
        "gemini": settings.gemini.model_dump(),
        "openai": {
            k: v
            for k, v in settings.openai.model_dump().items()
            if k not in {"rpm_buckets"}
        },
        "anthropic": settings.anthropic.model_dump(),
    }


def _parse_keys(raw: str) -> list[str]:
    try:
        keys = parse_credentials(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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


@app.get("/api/jobs/{job_id}/download/{index}")
async def download_report(job_id: str, index: int):
    """Return the full downloadable report for one key result (e.g. a GCP scan)
    that was too large to show inline in the remarks."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if index < 0 or index >= len(job.results):
        raise HTTPException(404, "result not found")
    r = job.results[index]
    if not r.download_text:
        raise HTTPException(404, "no downloadable report for this result")
    from fastapi.responses import Response

    filename = r.download_filename or f"report-{index}.txt"
    return Response(
        content=r.download_text,
        media_type="application/json"
        if filename.endswith(".json")
        else "text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
async def index():
    """Serve index.html with cache-busting version stamps on the JS/CSS URLs
    (derived from file mtime), so a browser can never run a stale bundle —
    the #1 cause of 'no plugin matched' after a UI change."""
    from fastapi.responses import HTMLResponse

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.css", "app.js"):
        try:
            ver = int((STATIC_DIR / asset).stat().st_mtime)
        except OSError:
            ver = 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html, headers=_NO_CACHE)


@app.get("/admin")
async def admin():
    """Serve admin panel for long-term key management."""
    from fastapi.responses import HTMLResponse

    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    for asset in ("admin.css", "admin.js"):
        try:
            ver = int((STATIC_DIR / asset).stat().st_mtime)
        except OSError:
            ver = 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    return HTMLResponse(html, headers=_NO_CACHE)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Local dev tool: never let the browser cache UI assets, so edits to the
    JS/CSS show up on a plain refresh instead of serving a stale bundle."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/admin")
async def admin_page():
    """Redirect /admin to admin interface"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/admin.html", status_code=302)


@app.get("/")
async def root_page():
    """Root endpoint - redirect to main UI"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/index.html", status_code=302)
