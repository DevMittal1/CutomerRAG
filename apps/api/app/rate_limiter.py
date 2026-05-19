import time
from typing import Tuple
from fastapi import HTTPException, Request, status
import redis.asyncio as redis
from app.config import settings

class SlidingWindowRateLimiter:
    """
    Asynchronous Sliding Window Rate Limiter using Redis.
    """
    def __init__(self, limit: int, window_seconds: int, redis_url: str):
        self.limit = limit
        self.window_seconds = window_seconds
        self.redis_client = redis.from_url(redis_url, decode_responses=True)

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
        redis_key = f"rate_limit:{client_ip}"

        async with self.redis_client.pipeline(transaction=True) as pipe:
            # 1. Remove timestamps older than cutoff
            pipe.zremrangebyscore(redis_key, "-inf", cutoff)
            # 2. Get current request count in the window
            pipe.zcard(redis_key)
            results = await pipe.execute()
        
        current_count = results[1]
        
        if current_count < self.limit:
            # Allowed: add current timestamp and update expiry
            async with self.redis_client.pipeline(transaction=True) as pipe:
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, self.window_seconds)
                await pipe.execute()
            
            remaining = self.limit - (current_count + 1)
            return False, remaining, 0
        else:
            # Rate limited: find the oldest timestamp to calculate retry_after
            oldest_element = await self.redis_client.zrange(redis_key, 0, 0, withscores=True)
            if oldest_element:
                oldest_timestamp = oldest_element[0][1]
                retry_after = int(self.window_seconds - (now - oldest_timestamp))
                retry_after = max(retry_after, 1)
            else:
                retry_after = 1
            return True, 0, retry_after

    async def close(self):
        await self.redis_client.aclose()


# Global rate limiter initialized with settings configurations
global_rate_limiter = SlidingWindowRateLimiter(
    limit=settings.RATE_LIMIT_REQUESTS,
    window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    redis_url=settings.REDIS_URI
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
