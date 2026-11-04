import logging

from app.core.text import redact

_FORMATTER = logging.Formatter()
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Exception messages can carry secrets: render the traceback once, redacted.
        if record.exc_info:
            record.exc_text = redact(_FORMATTER.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            # uvicorn's AccessFormatter unpacks (client, method, full_path, http_version,
            # status), so keep the tuple shape: drop the query, redact each string.
            client, method, path, version, status = args
            path = str(path).split("?", 1)[0]
            record.args = tuple(
                redact(a) if isinstance(a, str) else a
                for a in (client, method, path, version, status)
            )
            record.msg = redact(str(record.msg))
            return True
        record.msg = redact(record.getMessage())
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
