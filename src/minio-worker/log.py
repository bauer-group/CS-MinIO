"""Structured, readable logging via ``rich`` — mirrors the init container's style.

A redaction filter masks known secret values (CF / Bunny / webhook tokens) from
every log record so credentials can never leak, even inside an exception message.
"""

import logging

from rich.logging import RichHandler


class _RedactionFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            msg = record.getMessage()
            for secret in self._secrets:
                if secret in msg:
                    msg = msg.replace(secret, "***")
            record.msg = msg
            record.args = ()
        return True


def setup_logging(level: str = "INFO", secrets=()) -> logging.Logger:
    handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    handler.addFilter(_RedactionFilter(secrets))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[handler],
    )
    # Waitress logs a warning per request queue depth; keep it quiet unless debugging.
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    return logging.getLogger("minio-worker")
