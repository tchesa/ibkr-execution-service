"""Alerts: Slack incoming webhook plus an ``events`` row. Never depends on the broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from ibkr_exec.clock import Clock
from ibkr_exec.db.repo import Repository
from ibkr_exec.logging import get_logger

log = get_logger(__name__)

_EMOJI = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}


class Alerter:
    """Base: records the alert as an event, then delivers it via ``_send``."""

    def __init__(self, repo: Repository | None, clock: Clock) -> None:
        self._repo = repo
        self._clock = clock

    async def alert(self, level: str, title: str, body: str = "", **ctx: Any) -> None:
        now = self._clock.now()
        log.warning("alert", level=level, title=title, body=body, **ctx)
        if self._repo is not None:
            try:
                await self._repo.add_event(
                    now, "alert", f"{title}: {body}" if body else title, level=level, payload=ctx
                )
            except Exception as exc:  # the alert must still go out
                log.error("alert_event_write_failed", error=str(exc))
        try:
            await self._send(level, title, body, ctx)
        except Exception as exc:
            log.error("alert_send_failed", error=str(exc), title=title)

    async def _send(self, level: str, title: str, body: str, ctx: dict[str, Any]) -> None:
        raise NotImplementedError


class SlackAlerter(Alerter):
    def __init__(
        self, webhook_url: str, repo: Repository | None, clock: Clock, mode: str = "paper"
    ) -> None:
        super().__init__(repo, clock)
        self._url = webhook_url
        self._mode = mode

    async def _send(self, level: str, title: str, body: str, ctx: dict[str, Any]) -> None:
        if not self._url:
            log.error("slack_webhook_missing", title=title)
            return
        text = f"{_EMOJI.get(level, ':bell:')} *[{self._mode}] {title}*"
        if body:
            text += f"\n{body}"
        if ctx:
            text += "\n```" + "\n".join(f"{k}: {v}" for k, v in ctx.items()) + "```"
        delay = 1.0
        async with httpx.AsyncClient(timeout=10) as client:
            for attempt in range(3):
                try:
                    r = await client.post(self._url, json={"text": text})
                    if r.status_code < 300:
                        return
                    log.warning("slack_non_2xx", status=r.status_code, attempt=attempt)
                except httpx.HTTPError as exc:
                    log.warning("slack_http_error", error=str(exc), attempt=attempt)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("slack webhook failed after 3 attempts")


@dataclass
class RecordingAlerter(Alerter):
    """For tests: keeps every alert in memory."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def __init__(self, repo: Repository | None, clock: Clock) -> None:
        super().__init__(repo, clock)
        self.sent = []

    async def _send(self, level: str, title: str, body: str, ctx: dict[str, Any]) -> None:
        self.sent.append({"level": level, "title": title, "body": body, **ctx})

    def titles(self) -> list[str]:
        return [a["title"] for a in self.sent]
