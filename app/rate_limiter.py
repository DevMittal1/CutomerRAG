import asyncio
import time
from collections import defaultdict
from typing import Dict, List, Tuple
from fastapi import HTTPException, Request, status
from app.config import settings

class SlidingWindowRateLimiter:
    """
    Thread-safe, asynchronous in-memory sliding window rate limiter.
    Does not require external dependencies like Redis, keeping the deploy self-contained.
    """
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        # Maps client IP to list of request timestamps
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.lock = asyncio.Lock()

    async def is_rate_limited(self, client_ip: str) -> Tuple[bool, int, int]:
        """
        Evaluates the rate limit for a client IP.
        Returns:
            (is_limited, remaining_tokens, retry_after_seconds)
        """
        # IP Whitelist Bypass Check: Skip limits completely for administrative or internal IPs
        if client_ip in settings.get_bypass_ips():
            return False, self.limit, 0

        now = time.time()
        cutoff = now - self.window_seconds

        async with self.lock:
            # Prune timestamps older than the sliding window threshold
            self.requests[client_ip] = [t for t in self.requests[client_ip] if t > cutoff]
            
            history = self.requests[client_ip]
            current_count = len(history)
            
            if current_count < self.limit:
                # Request is allowed; record the current timestamp
                history.append(now)
                remaining = self.limit - (current_count + 1)
                return False, remaining, 0
            else:
                # Request is rate limited; calculate time to wait for the oldest timestamp to fall out
                oldest_timestamp = history[0]
                retry_after = int(self.window_seconds - (now - oldest_timestamp))
                # Ensure we wait at least 1 second
                retry_after = max(retry_after, 1)
                return True, 0, retry_after

# Global rate limiter initialized with settings configurations
global_rate_limiter = SlidingWindowRateLimiter(
    limit=settings.RATE_LIMIT_REQUESTS,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS
)

async def rate_limit_dependency(request: Request) -> None:
    """
    FastAPI dependency to enforce rate limiting on specific router endpoints.
    Retrieves the client's public IP address, handling proxies appropriately.
    """
    # Extract IP behind proxy/load balancers if present
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        client_ip = x_forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.client.host if request.client else "unknown"

    is_limited, remaining, retry_after = await global_rate_limiter.is_rate_limited(client_ip)
    
    # Store remaining quota in request state to optionally attach to response headers later
    request.state.rate_limit_remaining = remaining
    request.state.rate_limit_limit = settings.RATE_LIMIT_REQUESTS

    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please cool down before retrying.",
            headers={"Retry-After": str(retry_after)}
        )
