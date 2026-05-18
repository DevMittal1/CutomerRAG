import contextvars
import logging
import sys
import time
from typing import Any, Dict

# Context variable to hold the unique request ID for the duration of a request lifecycle
request_id_var = contextvars.ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    """
    Filter that injects the current request ID from the context variable into the log record.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True

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
