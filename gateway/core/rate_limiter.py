import threading
import time

from collections import defaultdict


class FixedWindowRateLimiter:
    def __init__(self, limit, window_size):
        if limit <= 0 or window_size <= 0:
            raise ValueError("Limit and window size must be positive")
        
        self.limit = limit
        self.window_size = window_size
        self.counters = defaultdict(lambda: {"window_start": 0, "count": 0})
        self.lock = threading.Lock()

    def check(self, client_id):
        with self.lock:
            current_time = int(time.time())
            window_start = current_time - (current_time % self.window_size)
            user_data = self.counters[client_id]

            if user_data["window_start"] != window_start:
                user_data["window_start"] = window_start
                user_data["count"] = 0

            if user_data["count"] >= self.limit:
                retry_after = window_start + self.window_size - current_time
                return True, retry_after

            user_data["count"] += 1
            return False, 0
