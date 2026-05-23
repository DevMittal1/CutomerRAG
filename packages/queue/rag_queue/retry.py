import random
import httpx

def should_retry_http(exception: Exception, attempt: int, max_retries: int) -> bool:
    """
    Standard policy determining if an HTTP error is transient and retryable.
    """
    if attempt >= max_retries:
        return False

    if isinstance(exception, httpx.HTTPStatusError):
        status_code = exception.response.status_code
        return status_code == 429 or status_code >= 500

    return isinstance(exception, (httpx.RequestError, httpx.TimeoutException))

def get_exponential_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
) -> float:
    """
    Computes exponential backoff delay with randomized jitter to prevent thundering herd issues.
    """
    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
    return delay * random.uniform(0.5, 1.5)
