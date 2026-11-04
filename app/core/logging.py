import logging

from core_engine.redaction import redact_text

_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            # uvicorn's AccessFormatter unpacks (client, method, full_path, http_version,
            # status), so keep the tuple shape: drop the query, redact each string.
            client, method, path, version, status = args
            path = str(path).split("?", 1)[0]
            record.args = tuple(
                redact_text(a) if isinstance(a, str) else a
                for a in (client, method, path, version, status)
            )
            record.msg = redact_text(str(record.msg))
            return True
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging() -> None:
    redacting = RedactingFilter()
    root = logging.getLogger()
    if not root.handlers:
        # ponytail: plain stderr handler so app.* logs never bypass the filter via lastResort.
        root.addHandler(logging.StreamHandler())
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        targets = [logger, *logger.handlers]
        for target in targets:
            if not any(isinstance(f, RedactingFilter) for f in target.filters):
                target.addFilter(redacting)
