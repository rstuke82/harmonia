"""Levelled, redacted runtime events shared by jobs, retained logs and console."""

import logging
import re

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def emit(*parts, level=None):
    """Retain messages once, with the same severity in jobs and diagnostics."""
    from . import jobs
    from . import console_logging
    from .diagnostics import redact

    message = _ANSI.sub("", " ".join(str(part) for part in parts))
    if not message.strip():
        return
    if level is None:
        level = (
            "ERROR"
            if re.match(r"(?i)^(?:✗|error\b|failed\b|could not\b)", message.lstrip())
            else "WARNING"
            if "⚠" in message or re.match(r"(?i)^warning\b", message.lstrip())
            else "INFO"
        )
    context = jobs.current()
    message = redact(message, context.redaction_config if context else None)
    jobs.output(message, persist=not console_logging.web_mode)
    if console_logging.web_mode:
        from .api import _record_log

        _record_log(
            level,
            "Runtime",
            message,
            context.redaction_config if context else None,
            _console=console_logging.enabled or level in {"WARNING", "ERROR"},
        )
    elif not context:
        logging.getLogger("harmonia.runtime").log(getattr(logging, level), "%s", message)
