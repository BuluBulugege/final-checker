"""CLI entry point: read keys from a file (or stdin), run checks, print a table.

    final-checker keys.txt --mode grade --concurrency 5
    cat keys.txt | final-checker - --mode health
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import settings
from app.jobs import manager
from app.models import CheckMode, JobRequest, KeyStatus


def main() -> None:
    parser = argparse.ArgumentParser(description="Pluggable API key checker")
    parser.add_argument("keyfile", help="path to keys file (one per line), or - for stdin")
    parser.add_argument(
        "--mode", choices=[m.value for m in CheckMode], default=CheckMode.GRADE.value
    )
    parser.add_argument("--concurrency", type=int, default=settings.default_concurrency)
    parser.add_argument(
        "--no-full-load",
        action="store_true",
        help="use gentle probe rates to save cost",
    )
    args = parser.parse_args()

    if args.keyfile == "-":
        raw = sys.stdin.read()
    else:
        with open(args.keyfile, encoding="utf-8") as keyfile:
            raw = keyfile.read()
    req = JobRequest(
        keys=raw,
        mode=CheckMode(args.mode),
        concurrency=args.concurrency,
        full_load=not args.no_full_load,
    )
    asyncio.run(_run(req))


async def _run(req: JobRequest) -> None:
    from app.parsing import parse_credentials

    try:
        keys = parse_credentials(req.keys)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    if not keys:
        print("no keys provided", file=sys.stderr)
        sys.exit(1)
    job = manager.create(req, keys)
    assert job._task is not None
    await job._task

    print(f"\n{'#':<3} {'provider':<10} {'status':<11} {'tier':<10} masked")
    print("-" * 70)
    for r in job.results:
        print(
            f"{r.index:<3} {(r.provider or '-'):<10} {r.status.value:<11} "
            f"{(r.tier or '-'):<10} {r.masked_key}"
        )
        for note in r.remarks:
            print(f"      ↳ {note}")
        if r.error:
            print(f"      ! {r.error}")
        # Persist any large downloadable report (e.g. a GCP scan) next to cwd.
        if r.download_text and r.download_filename:
            with open(r.download_filename, "w", encoding="utf-8") as fh:
                fh.write(r.download_text)
            print(f"      ⬇ 完整报告已保存: {r.download_filename}")

    graded = sum(1 for r in job.results if r.status in {KeyStatus.GRADED, KeyStatus.ALIVE})
    print(f"\n{graded}/{len(job.results)} keys checked OK")


if __name__ == "__main__":
    main()
