"""Shared console and job output routing."""


def print(*args, **kwargs):
    """Route runtime messages to the configured console and current job."""
    import builtins
    from . import jobs
    from . import console_logging
    message=kwargs.get('sep', ' ').join(str(a) for a in args)
    if not console_logging.web_mode or console_logging.enabled:
        builtins.print(*args, **kwargs)
    if jobs.current():
        jobs.output(message)
    elif console_logging.web_mode and message.strip():
        # Web-mode output must never vanish merely because it was emitted
        # outside a worker context.
        from .api import _record_log
        _record_log('INFO','Runtime',message)


