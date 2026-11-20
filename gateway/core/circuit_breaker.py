import threading
import time

from enum import Enum


class CircuitState(Enum):
    OPEN = "open"
    HALF_OPEN = "half_open"
    CLOSED = "closed"


class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.state = CircuitState.CLOSED
        self.probe_in_progress = False
        self.open_until = 0
        self.lock = threading.Lock()
        self.recovery_timeout = recovery_timeout
        self.failure_threshold = failure_threshold
        self.failures = 0

    def abort_request(self):
        with self.lock:
            self.probe_in_progress = False

    def record_failure(self):
        with self.lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.open_until = time.monotonic() + self.recovery_timeout
                self.probe_in_progress = False

    def record_success(self):
        with self.lock:
            self.failures = 0
            self.state = CircuitState.CLOSED
            self.open_until = 0
            self.probe_in_progress = False

    def allow_request(self):
        with self.lock:
            if self.state == CircuitState.OPEN:
                if time.monotonic() < self.open_until:
                    return False
                self.state = CircuitState.HALF_OPEN

            if self.state == CircuitState.HALF_OPEN:
                if self.probe_in_progress:
                    return False

                self.probe_in_progress = True

            return True
