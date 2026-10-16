"""Runtime console verbosity; retained web job output is unaffected."""

from copy import deepcopy
import logging

enabled = False
web_mode = False


def uvicorn_log_config():
    """Keep Uvicorn's normal output while dating every console line."""
    from uvicorn.config import LOGGING_CONFIG

    config = deepcopy(LOGGING_CONFIG)
    for name in ("default", "access"):
        formatter = config["formatters"][name]
        formatter["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
        formatter["datefmt"] = "%Y-%m-%dT%H:%M:%S%z"
    return config


class AccessFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            _method, status = args[1], args[4]
            status = int(status)
            level = (
                logging.ERROR
                if status >= 500
                else logging.WARNING
                if status >= 400
                else logging.DEBUG
            )
            if level == logging.DEBUG and not enabled:
                return False
            record.levelno = level
            record.levelname = logging.getLevelName(level)
        return True


def configure(repo):
    global enabled, web_mode
    enabled = bool(repo.load("console_settings").get("debug", False))
    web_mode = True
    if not enabled:
        repo.clear_debug_logs()
    logging.getLogger("uvicorn.error").setLevel(logging.DEBUG if enabled else logging.INFO)
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessFilter) for f in logger.filters):
        logger.addFilter(AccessFilter())


def register(app):
    from pydantic import BaseModel
    from .api import job_store

    class ConsoleSettings(BaseModel):
        debug: bool = False

    @app.get("/api/settings/console")
    def get():
        return {"debug": bool(job_store().repository.load("console_settings").get("debug", False))}

    @app.put("/api/settings/console")
    def put(request: ConsoleSettings):
        repo = job_store().repository
        repo.save({"console_settings": request.model_dump()})
        configure(repo)
        return request.model_dump()
