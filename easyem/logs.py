"""Structured logging.

The request middleware already passes request_id, path and duration as extra
fields. Without a formatter that renders them, they vanish and production logs
are prose. This is the formatter.
"""

from __future__ import annotations

import json
import logging
import sys

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime", "message", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Anything the caller passed via extra=
        payload.update(
            {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output
        else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
