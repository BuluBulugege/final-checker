"""Reusable async burst rate-tester. Fires a probe coroutine at a target rate
(requests/second) for a fixed duration, or until a cap of total requests, and
records when the first 429 (true rate limit) arrives.

This is the shared engine behind every plugin's grade probe:
- Gemini:  50 rps for 30s, no cap, observe first-429 timing.
- OpenAI:  30 rps capped at ~40 calls, derive max measured rpm.

The scheduler dispatches requests on a fixed cadence (1/target_rps apart) and
lets them run concurrently, so a slow individual request never throttles the
send rate. A monotonic clock is used so timing is robust.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.models import BurstResult, ErrorClass, ProbeOutcome
from app.redact import redact

# A probe is an async callable that performs ONE request and returns its outcome.
Probe = Callable[[], Awaitable[ProbeOutcome]]


async def run_burst(
    probe: Probe,
    *,
    target_rps: float,
    duration_s: float,
    cap: int | None = None,
    stop_on_first_429: bool = False,
    on_tick: Callable[[float], Awaitable[None]] | None = None,
) -> BurstResult:
    """Run a burst test.

    Args:
        probe: async callable performing a single request.
        target_rps: requests per second to schedule.
        duration_s: how long to keep dispatching.
        cap: optional max number of requests to dispatch (whichever comes first).
        stop_on_first_429: if True, stop dispatching new requests once a 429 is seen.
        on_tick: optional async callback(elapsed_fraction_0_to_1) for progress.

    Returns:
        BurstResult with counts and first-429 timing (seconds from start).
    """
    result = BurstResult(target_rps=target_rps, cap=cap)
    interval = 1.0 / target_rps if target_rps > 0 else 0.0
    start = time.monotonic()
    first_429_lock = asyncio.Lock()
    stop_flag = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    error_counts: dict[str, int] = {}

    async def one_request() -> None:
        try:
            outcome = await probe()
        except Exception as exc:  # genuine probe bug / unexpected — count as error
            outcome = ProbeOutcome(
                ok=False, error_class=ErrorClass.NETWORK, detail=redact(repr(exc))
            )
        # Time the 429 from when its RESPONSE arrives, not when we dispatched, so
        # the first-429 timing reflects real round-trip latency under load.
        done_at = time.monotonic()
        result.total_sent += 1
        if outcome.error_class == ErrorClass.RATE_LIMIT:
            result.rate_limited += 1
            async with first_429_lock:
                t = done_at - start
                if result.first_429_at_s is None or t < result.first_429_at_s:
                    result.first_429_at_s = round(t, 3)
            if stop_on_first_429:
                stop_flag.set()
        elif outcome.ok:
            result.success += 1
        else:
            result.other_errors += 1
        key = outcome.error_class.value
        error_counts[key] = error_counts.get(key, 0) + 1

    # Dispatch loop on a fixed cadence.
    n_dispatched = 0
    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed >= duration_s:
            break
        if cap is not None and n_dispatched >= cap:
            break
        if stop_flag.is_set():
            break
        tasks.append(asyncio.create_task(one_request()))
        n_dispatched += 1
        if on_tick is not None:
            frac = min(elapsed / duration_s, 0.99) if duration_s > 0 else 0.0
            await on_tick(frac)
        # sleep until the next scheduled send, accounting for time already spent
        if interval > 0:
            next_at = start + n_dispatched * interval
            sleep_for = next_at - time.monotonic()
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(stop_flag.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

    # Wait for all in-flight requests to settle.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_s = round(time.monotonic() - start, 3)
    result.error_breakdown = error_counts

    # Derive measured rpm for a capped probe (image-style "max rpm" probe).
    # measured_rpm is the successful-request throughput scaled to requests-per-
    # minute, so it is directly comparable to documented per-minute ceilings and
    # to the rpm bucket thresholds. Both branches scale consistently.
    if cap is not None:
        if result.first_429_at_s is None:
            # never hit a limit within the cap — the true ceiling is AT LEAST this.
            window = result.duration_s if result.duration_s > 0 else None
            if window:
                result.measured_rpm = round(result.success / window * 60.0, 1)
            result.notes.append(
                f"no 429 within cap={cap}; measured ceiling >= {result.success} "
                f"reqs in {result.duration_s}s (>= {result.measured_rpm} rpm)"
            )
        else:
            # successes before the limit kicked in, scaled over the window up to
            # the first 429, give the empirical per-minute ceiling.
            window = result.first_429_at_s if result.first_429_at_s > 0 else result.duration_s
            if window and window > 0:
                result.measured_rpm = round(result.success / window * 60.0, 1)
            else:
                result.measured_rpm = float(result.success)
            result.notes.append(
                f"429 after {result.success} successful reqs (~{result.first_429_at_s}s) "
                f"=> ~{result.measured_rpm} rpm"
            )
    if on_tick is not None:
        await on_tick(1.0)
    return result
