import json
import logging
import sys
from typing import Any, Dict, Optional


class StructuredWorkerLogger:
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
