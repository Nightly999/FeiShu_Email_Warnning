from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.bootstrap import bootstrap
from app.event_dedup import claim_event, finish_event
from app.settings import get_settings


@dataclass
class BurstResult:
    requested: int
    accepted: int
    dropped: int
    workers: int
    queue_size: int
    simulated_task_seconds: float
    elapsed_seconds: float
    wait_p50_ms: float
    wait_p95_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_max_ms: float


@dataclass
class DatabaseResult:
    concurrency: int
    successes: int
    errors: int
    elapsed_seconds: float
    operations_per_second: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_max_ms: float


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * percent), len(ordered) - 1)
    return ordered[index]


def benchmark_worker_burst(
    requested: int,
    *,
    workers: int,
    queue_size: int,
    simulated_task_seconds: float,
) -> BurstResult:
    slots = threading.BoundedSemaphore(workers + queue_size)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="load-test")
    measurements: list[tuple[float, float]] = []
    measurements_lock = threading.Lock()
    futures: list[Future[None]] = []
    dropped = 0
    burst_started = time.perf_counter()

    def task(submitted_at: float) -> None:
        started_at = time.perf_counter()
        time.sleep(simulated_task_seconds)
        finished_at = time.perf_counter()
        with measurements_lock:
            measurements.append((started_at - submitted_at, finished_at - submitted_at))

    def release_slot(_future: Future[None]) -> None:
        slots.release()

    for _ in range(requested):
        submitted_at = time.perf_counter()
        if not slots.acquire(blocking=False):
            dropped += 1
            continue
        future = executor.submit(task, submitted_at)
        future.add_done_callback(release_slot)
        futures.append(future)

    for future in futures:
        future.result()
    executor.shutdown(wait=True)
    elapsed = time.perf_counter() - burst_started
    waits = [item[0] * 1000 for item in measurements]
    latencies = [item[1] * 1000 for item in measurements]
    return BurstResult(
        requested=requested,
        accepted=len(futures),
        dropped=dropped,
        workers=workers,
        queue_size=queue_size,
        simulated_task_seconds=simulated_task_seconds,
        elapsed_seconds=round(elapsed, 3),
        wait_p50_ms=round(statistics.median(waits), 2) if waits else 0.0,
        wait_p95_ms=round(percentile(waits, 0.95), 2),
        latency_p50_ms=round(statistics.median(latencies), 2) if latencies else 0.0,
        latency_p95_ms=round(percentile(latencies, 0.95), 2),
        latency_max_ms=round(max(latencies), 2) if latencies else 0.0,
    )


async def benchmark_database(concurrency: int) -> DatabaseResult:
    latencies: list[float] = []
    errors: list[str] = []

    async def operation(index: int) -> None:
        started = time.perf_counter()
        scope = {
            "tenant_key": "load-test-tenant",
            "app_id": "load-test-app",
            "message_id": f"load-test-message-{concurrency}-{index}",
        }
        try:
            if not await claim_event(**scope):
                raise RuntimeError("unique event was not claimed")
            await finish_event(**scope)
            latencies.append((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    started = time.perf_counter()
    await asyncio.gather(*(operation(index) for index in range(concurrency)))
    elapsed = time.perf_counter() - started
    return DatabaseResult(
        concurrency=concurrency,
        successes=len(latencies),
        errors=len(errors),
        elapsed_seconds=round(elapsed, 3),
        operations_per_second=round(len(latencies) / elapsed, 2) if elapsed else 0.0,
        latency_p50_ms=round(statistics.median(latencies), 2) if latencies else 0.0,
        latency_p95_ms=round(percentile(latencies, 0.95), 2),
        latency_max_ms=round(max(latencies), 2) if latencies else 0.0,
    )


async def verify_duplicate_claim() -> dict[str, int]:
    scope = {
        "tenant_key": "load-test-tenant",
        "app_id": "load-test-app",
        "message_id": "same-message-id",
    }
    claims = await asyncio.gather(*(claim_event(**scope) for _ in range(50)))
    if any(claims):
        await finish_event(**scope)
    return {"attempts": len(claims), "successful_claims": sum(claims)}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    burst_results = [
        benchmark_worker_burst(
            users,
            workers=settings.feishu_event_workers,
            queue_size=settings.feishu_event_queue_size,
            simulated_task_seconds=args.simulated_task_seconds,
        )
        for users in args.concurrency
    ]

    previous = {
        key: os.environ.get(key)
        for key in ("APP_DATABASE_PATH", "FEISHU_APPS_CONFIG_PATH", "APP_ENV")
    }
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        os.environ["APP_DATABASE_PATH"] = str(Path(temp_dir) / "load-test.db")
        os.environ["FEISHU_APPS_CONFIG_PATH"] = str(Path(temp_dir) / "missing-apps.json")
        os.environ["APP_ENV"] = "production"
        get_settings.cache_clear()
        try:
            await bootstrap()
            database_results = [
                await benchmark_database(users) for users in args.concurrency
            ]
            duplicate_result = await verify_duplicate_claim()
        finally:
            get_settings.cache_clear()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "bot_processes": 9,
            "workers_per_bot": settings.feishu_event_workers,
            "queue_per_bot": settings.feishu_event_queue_size,
        },
        "worker_burst": [asdict(item) for item in burst_results],
        "sqlite_idempotency": [asdict(item) for item in database_results],
        "duplicate_claim": duplicate_result,
        "notes": [
            "No Feishu, LLM, MCP, or business database requests were made.",
            "Worker task duration is simulated; upstream service limits require a separate live test.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe local load test for the Feishu agent.")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[20, 50, 100])
    parser.add_argument("--simulated-task-seconds", type=float, default=0.2)
    parser.add_argument("--output", type=Path, default=Path("data/load_test_report.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
