# M3 findings: timeouts and circuit breaker

## Setup

- Apple M2 Pro (16 GB RAM), Docker Engine 25.0.3, k6 v2.4.0, run on November 22, 2026.
- The stack ran with `docker compose`, with `docker-compose.loadtest.yml` raising the GET rate limit so 429s do not hide the circuit behavior.
- Breaker settings: 5 consecutive failures open the circuit for 30 s. Timeouts: connect 2 s, read 5 s.
- Scenario (`loadtest/circuit.js`):
  - **failing:** 5 req/s for 20 s to `/api/orders/error/?error_code=500`.
  - **recovery:** starts at 35 s and sends 5 req/s for 10 s to the healthy `/api/orders/1/`.
- The gateway was restarted before each run. Upstream calls were counted from the order service access log.

The commands to reproduce are in the README under "Circuit breaker scenario".

## Results

| | Breaker enabled | Breaker disabled |
| --- | --- | --- |
| Failing phase requests | 101 | 101 |
| `500` from upstream | 5 | 101 |
| `503` from the gateway (circuit open) | 96 | 0 |
| **Calls reaching the failing upstream** | **5** | **101** |
| Recovery phase `200` | 51 / 51 | 51 / 51 |

Raw summaries: `loadtest/results-circuit-on.json`, `loadtest/results-circuit-off.json`.

## Observations

- **Before opening (CLOSED):** the first 5 requests reached the upstream and returned its `500`. At 5 req/s, the circuit opened within about 1 second.
- **While open (OPEN):** the remaining 96 requests got an immediate `503` from the gateway, and the upstream received no calls. With the breaker disabled, all 101 requests hit the failing upstream, a 20x difference.
- **Recovery:** nothing happens when the 30 s window ends. The first request after that point moves the circuit to HALF_OPEN and goes through as a probe. Because the upstream was healthy, the probe succeeded, the circuit closed, and all 51 recovery requests returned `200`. If the probe had failed, the circuit would have reopened for another 30 s.
- **M1/M2 still work** with normal settings: `GET /api/users/1/` and `GET /api/orders/1/` return `200`, and the 31st GET within a minute returns `429` with a `Retry-After` header.
- Unit tests (`gateway/core/tests.py`) cover the same behavior with mocked time and HTTP: an open circuit does not call the upstream, and a disabled breaker forwards every request.

## Upstream errors vs. gateway errors

Observed with `curl` against the running stack:

| Situation | Status | Body | Produced by |
| --- | --- | --- | --- |
| Upstream returns an error | `500` / `503` | `{"service": "order-service", "error": true}` | Upstream (forwarded as is) |
| Upstream slower than the read timeout (`slow/?seconds=10`) | `504` | `{"detail": "Upstream timeout"}` | Gateway |
| Upstream stopped (`docker compose stop order_service`) | `502` | `{"detail": "Upstream connection error"}` | Gateway |
| Connect timeout (from code, not reproduced) | `502` | `{"detail": "Connection timed out."}` | Gateway |
| Circuit open | `503` | `{"detail": "Service temporarily unavailable."}` | Gateway |
| Rate limit exceeded | `429` | `{"detail": "Rate limit exceeded. ..."}` | Gateway |

The body tells the two sources apart: gateway errors always use `{"detail": ...}`. An upstream `503` and an open-circuit `503` have the same status code and differ only in the body. Before this change, the open-circuit response was plain text. It now uses the same JSON format as the other gateway errors.

## Known limitations

- Circuit state lives in process memory. Each gateway process or instance has its own circuit, and a restart resets it.
- The threshold (5) and open duration (30 s) are hard-coded.
- Failures must be consecutive. One success resets the count, so an upstream that fails 80% of the time may never open the circuit.
- There is no manual reset or force-open. `CIRCUIT_BREAKER_ENABLED` is read at startup and needs a restart.
- Requests rejected during HALF_OPEN (while the probe is in flight) also get `503`.
- Client-side cancellation and connect timeouts were not reproduced under load.
- These are single local runs.

## Questions for M3 UNDERSTAND / IMPROVE

1. Should the circuit open on a failure rate over a time window instead of consecutive failures, and should the threshold and duration be configurable?
2. With several gateway instances, should circuit state be shared (for example in Redis), or is per-instance state good enough?
