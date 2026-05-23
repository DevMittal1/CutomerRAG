import contextvars
import json
import logging
import sys
from typing import Any, Dict, Optional

# Context variable to hold the unique request ID for the duration of a request lifecycle
request_id_var = contextvars.ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    """
    Filter that injects the current request ID from the context variable into the log record.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True

class StructuredWorkerLogger:
    """
    Production-grade logger that supports structured 'extra' metadata.
    """
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)

    def _log(
        self,
        level: int,
        msg: str,
        extra: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        if extra:
            msg = f"{msg} | metadata={json.dumps(extra)}"
        self.logger.log(level, msg, **kwargs)

    def info(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs):
        self._log(logging.INFO, msg, extra, **kwargs)

    def warning(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs):
        self._log(logging.WARNING, msg, extra, **kwargs)

    def error(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs):
        self._log(logging.ERROR, msg, extra, **kwargs)

    def exception(self, msg: str, extra: Optional[Dict[str, Any]] = None, **kwargs):
        self._log(logging.ERROR, msg, extra, exc_info=True, **kwargs)

def setup_worker_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)

def get_worker_logger(name: str) -> StructuredWorkerLogger:
    return StructuredWorkerLogger(name)

def setup_logging() -> None:
    """
    Configures standard structured logging for the application.
    """
    # Create the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear existing handlers
    root_logger.handlers = []

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    # Create custom format
    # Example format: [2026-05-18 22:15:30.123] [INFO] [app.main:45] [req-ab12cd34] - Request completed in 15ms
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] [req_id=%(request_id)s] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(formatter)

    # Add the RequestIdFilter to inject correlation IDs
    console_handler.addFilter(RequestIdFilter())

    # Add handler to root logger
    root_logger.addHandler(console_handler)

    # Reduce log level for noisier external dependencies
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

def get_logger(name: str) -> logging.Logger:
    """
    Helper to fetch a logger with the given name.
    """
    return logging.getLogger(name)
