"""APScheduler wiring. Jobs run in New York time; each one alerts on exceptions instead
of dying quietly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ibkr_exec.clock import NY
from ibkr_exec.logging import get_logger
from ibkr_exec.service import Service

log = get_logger(__name__)


def _guarded(svc: Service, name: str, fn: Callable[[], Awaitable[object]]):
    async def run() -> None:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            log.error("job_failed", job=name, error=str(exc))
            try:
                await svc.alerter.alert("critical", f"Job failed: {name}", str(exc))
            except Exception:  # noqa: BLE001
                pass

    return run


def build_scheduler(svc: Service) -> AsyncIOScheduler:
    s = AsyncIOScheduler(timezone=NY)
    j = svc.cfg.jobs
    s.add_job(
        _guarded(svc, "keepalive", svc.job_keepalive),
        IntervalTrigger(seconds=60),
        id="keepalive",
        max_instances=1,
        coalesce=True,
    )
    s.add_job(
        _guarded(svc, "reconcile", svc.job_reconcile),
        IntervalTrigger(seconds=60),
        id="reconcile",
        max_instances=1,
        coalesce=True,
    )
    s.add_job(
        _guarded(svc, "stale_entries", svc.job_stale_entries),
        IntervalTrigger(seconds=60),
        id="stale_entries",
        max_instances=1,
        coalesce=True,
    )
    # Flatten fires every minute in the last trading hours; the job itself checks whether
    # this minute is flat_by (or the equivalent lead time before an early close).
    s.add_job(
        _guarded(svc, "flatten", svc.job_flatten),
        CronTrigger(day_of_week="mon-fri", hour="12-15", minute="*", timezone=NY),
        id="flatten",
        max_instances=1,
        coalesce=True,
    )
    s.add_job(
        _guarded(svc, "eod", svc.job_eod),
        CronTrigger(
            day_of_week="mon-fri", hour=j.eod_check.hour, minute=j.eod_check.minute, timezone=NY
        ),
        id="eod",
        max_instances=1,
        coalesce=True,
    )
    # Early-close days close at 13:00: run the EOD check at 13:10 as well (job is idempotent).
    s.add_job(
        _guarded(svc, "eod_early", svc.job_eod),
        CronTrigger(day_of_week="mon-fri", hour=13, minute=10, timezone=NY),
        id="eod_early",
        max_instances=1,
        coalesce=True,
    )
    s.add_job(
        _guarded(svc, "preopen", svc.job_preopen),
        CronTrigger(
            day_of_week="mon-fri",
            hour=j.preopen_check.hour,
            minute=j.preopen_check.minute,
            timezone=NY,
        ),
        id="preopen",
        max_instances=1,
        coalesce=True,
    )
    return s
