import httpx
import redis.asyncio as redis

from django.conf import settings


_client: httpx.AsyncClient | None = None

def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.UPSTREAM_CONNECT_TIMEOUT,
                read=settings.UPSTREAM_READ_TIMEOUT,
                write=settings.UPSTREAM_READ_TIMEOUT,
                pool=settings.UPSTREAM_CONNECT_TIMEOUT,
            ),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            trust_env=False
        )
    return _client

_redis: redis.Redis | None = None

def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.REDIS_URL)
    return _redis
