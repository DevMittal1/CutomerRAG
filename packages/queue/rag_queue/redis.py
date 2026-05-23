from redis.asyncio import Redis

def get_redis_client(redis_uri: str) -> Redis:
    """
    Initializes and returns an asynchronous Redis client with decode_responses=True.
    """
    return Redis.from_url(redis_uri, decode_responses=True)
