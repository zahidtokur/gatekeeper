import time

from core.http import get_redis


class FixedWindowRateLimiter:
    def __init__(self, name, limit, window_size):
        if limit <= 0 or window_size <= 0:
            raise ValueError("Limit and window size must be positive")

        self.name = name
        self.limit = limit
        self.window_size = window_size

    async def check(self, client_id):
        current_time = int(time.time())
        window_start = current_time - (current_time % self.window_size)
        key = f"ratelimit:{self.name}:{client_id}:{window_start}"

        redis = get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, self.window_size)

        if count > self.limit:
            retry_after = window_start + self.window_size - current_time
            return True, retry_after
        return False, 0
