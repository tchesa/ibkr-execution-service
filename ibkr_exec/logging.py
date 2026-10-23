"""structlog JSON logging with secret redaction."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import structlog

_SECRET_KEYS = {
    "tws_password",
    "password",
    "database_url",
    "slack_webhook_url",
    "hmac_bot_secret",
    "hmac_admin_secret",
    "secret",
    "signature",
    "authorization",
}
_URL_CREDS = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")
_SLACK = re.compile(r"https://hooks\.slack\.com/services/\S+")


def _redact_value(v: Any) -> Any:
    if isinstance(v, str):
        v = _URL_CREDS.sub(r"\1***@", v)
        v = _SLACK.sub("https://hooks.slack.com/services/***", v)
    return v


def redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict):
        if k.lower() in _SECRET_KEYS:
            event_dict[k] = "***"
        else:
            event_dict[k] = _redact_value(event_dict[k])
    return event_dict


def setup_logging(level: str | None = None) -> None:
    lvl = getattr(logging, (level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    pretty = sys.stderr.isatty() and os.environ.get("LOG_FORMAT", "") != "json"
    renderer: Any = (
        structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=lvl, stream=sys.stdout, format="%(message)s")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
