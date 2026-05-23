import time
from contextlib import contextmanager
from typing import Generator
from .logging import get_logger

logger = get_logger("observability.tracing")

@contextmanager
def trace_span(name: str) -> Generator[None, None, None]:
    """
    Context manager to trace the elapsed execution time of a code block.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        logger.info(f"Span '{name}' completed in {elapsed_ms:.2f}ms")
