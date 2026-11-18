# Gatekeeper

Three independent Django projects for learning how to build an API gateway: gateway, user_service, and order_service. The stack uses Python 3.13 and Daphne/ASGI. A shared Dockerfile builds a separate image for each service.

> **Note:** This is a *local reverse proxy for learning purposes*. It is not ready for production use.

## Architecture

```
client ──> gateway (:8000) ──/api/users/...──> user_service (:8001) /v1/users/...
                           └─/api/orders/...─> order_service (:8002) /v1/orders/...
```

- The gateway matches `/api/<service>/<path>` against a static route table (`gateway/core/views.py`) and forwards the request with a shared async HTTPX client.
- The path gets a trailing slash. Query parameters, the request body, and `Content-Type` are forwarded. No other client headers are forwarded.
- The upstream status code, body, and `Content-Type` are returned unchanged.
- An unknown service returns `404`. An upstream connection error returns `502`, and an upstream timeout returns `504` (2 s to connect, 5 s for each read/write).

## Client identification and rate-limit policy

The gateway enforces a fixed-window rate limiter whose counters are stored in Redis, so limits are shared by all gateway processes and survive a gateway restart.

For local development, the gateway uses the client's resolved IP address as its identifier. Requests with different source IP addresses belong to different clients. Requests sharing an IP address, including clients behind the same NAT, share the same allowance. To test two independent clients, use two distinct source IP addresses; separate processes on the same IP do not count as separate clients.

This identifier is **not authentication** and provides no proof of identity. IP-based identification can be spoofed, particularly if untrusted forwarding headers are accepted. Do not treat an arbitrary client-supplied IP header as a verified identity.

The default policy uses a fixed 60-second window and separate counters for each client and HTTP method:

| Method | Requests allowed per client per 60-second window |
| --- | --- |
| GET | 30 |
| POST | 10 |

These limits cover all proxied routes under `/api/users/` and `/api/orders/`, including nested paths. Each client's GET allowance is shared across both services, and its POST allowance is also shared across both services. Switching services or paths does not provide a fresh allowance. GET requests do not consume the POST allowance, and vice versa. Health checks at `/health/` and non-proxy routes are outside this policy; methods other than GET and POST remain unsupported.

If the gateway cannot determine a client's IP address, it rejects the request with **503 Service Unavailable** rather than forward it or assign a shared anonymous identifier:

```json
{"detail": "Unable to identify client"}
```

### Rate-limited response

When a client exceeds its allowance, the gateway returns **429 Too Many Requests** and does not forward the request upstream:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 17
Content-Type: application/json

{"detail": "Rate limit exceeded. Retry after the time in the Retry-After header."}
```

`Retry-After` is the number of whole seconds until the current fixed window ends, between 1 and 60. Counters reset at window boundaries aligned to the clock (for example, :00 of each minute), not 60 seconds after the client's first request.

All gateway-generated errors (429, 502, 503, 504) use the same `{"detail": "..."}` body.

## Running the project

Docker Desktop must be running with Linux containers enabled. Docker Compose v2 is required. From the project root:

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f
```

On startup, each Django service runs migrations against its own database, then starts Daphne. The gateway waits for the user and order services and Redis to pass their health checks.

| Service | Address | Data volume |
| --- | --- | --- |
| gateway | http://localhost:8000 | gateway_data |
| user_service | http://localhost:8001 | user_service_data |
| order_service | http://localhost:8002 | order_service_data |
| redis | redis:6379 within the Docker network | redis_data |


## Rate-limit state in Redis

The gateway reads the Redis address from `REDIS_URL` (`redis://redis:6379/0` in Docker Compose). Redis starts with the rest of the stack (`docker compose up -d`), and the gateway waits for it to be healthy. Redis is reachable only inside the Docker network; use `docker compose exec redis redis-cli` to inspect it.

Each counter is one key, `ratelimit:<limiter>:<client IP>:<window start>`, for example `ratelimit:post:172.21.0.1:1794926520`. For a newly created counter, the gateway separately assigns a 60-second TTL after incrementing it. Expiration is measured from that operation, not from the fixed window boundary, so an old key may remain after its counting window ends. Because the window start is part of the key, a new window automatically uses a new counter. Expiry assignment can fail independently of the increment; see the atomicity limitations below.

```sh
docker compose exec redis redis-cli --scan --pattern 'ratelimit:*'   # list counters
docker compose exec redis redis-cli GET <key>                         # requests counted in the window
docker compose exec redis redis-cli TTL <key>                         # seconds until the key expires
```

To reset all rate limits, for example after manual testing:

```sh
docker compose exec redis redis-cli --scan --pattern 'ratelimit:*' | xargs docker compose exec -T redis redis-cli DEL
# or wipe the whole Redis database:
docker compose exec redis redis-cli FLUSHDB
```

Restarting the gateway (`docker compose restart gateway`) does not reset an active limit. Redis runs with append-only persistence, so counters also survive a Redis restart until they expire.

## Rate-limit observations with two gateway instances

A manual integration check was performed with two independent gateway containers exposed on host ports 8000 and 8003. Both ran on the same Docker network and used the same Redis database. Matching Redis server identities and connection settings confirmed that the gateways shared the same backing store.

Requests from one client IP were distributed between the two instances. GET requests alternated between user and order routes to check whether switching gateways or upstream services provided a separate allowance. POST requests were then sent through both instances to observe whether their allowance remained independent of the exhausted GET limit.

The check also inspected Redis counters and expiry values, verified that requests were accepted again after the next fixed-window boundary, and sent a concurrent batch through both gateways to observe the combined number of accepted requests. Each batch stayed within its counting window, and fresh windows were used without deleting existing Redis data. The check used real HTTP requests and Redis operations, without mocks.

### Observed results

The check was run locally on November 18, 2026, against ports 8000 and 8003. Both gateways reported `redis://redis:6379/0` and the same Redis run ID. The observed peer IP was `172.18.0.1` (this address may differ on another machine).

| Check | Observed result |
| --- | --- |
| 32 GET requests alternating between instances and user/order routes | First 30 returned 200; requests 31 and 32 returned 429, one on each instance |
| 12 POST requests alternating between instances, after exhausting GET | First 10 returned 200; requests 11 and 12 returned 429 |
| Redis counters after these batches | One GET key with value 32 and one POST key with value 12 for the same IP and window; both had a TTL of 59 seconds when inspected |
| Rate-limit response headers | Rejected requests contained `Retry-After`; observed values were 60 and 59 seconds |
| First GET through each instance in the next window | Both returned 200 |
| 40 concurrent GETs across both instances after those two accepted GETs | 28 returned 200 and 12 returned 429, giving 30 accepted GETs in total for that window |

The allowance was shared across gateway processes and upstream services, while GET and POST used independent counters. Rejected attempts also increased the Redis counter: its value represents attempts, not just requests forwarded upstream. A fresh window restored the allowance without restarting either gateway. The concurrent batch did not exceed the configured limit in this run; it does not prove that all failure scenarios are safe.

### Current atomicity limitations

Redis increments each counter atomically, so simultaneous requests to the same key receive distinct counts. However, incrementing the counter and assigning its expiry are two separate operations in the current implementation.

If the gateway stops, its request is cancelled, or the connection fails after the first increment but before expiry is assigned, the counter can remain without a TTL. Later requests increase its value beyond one and therefore do not assign the missing expiry. Such keys can accumulate indefinitely and consume Redis memory. Because the key includes the window start, a later window uses a different key: the abandoned counter does not permanently block the client.

A delay between the first increment and expiry assignment also delays cleanup relative to the original request. In addition, a Redis command can succeed before a connection failure prevents the gateway from receiving its result. A request may then consume allowance even though the gateway returns an error and never forwards it; retrying that HTTP request can consume another count. Redis errors from the limiter currently propagate without a dedicated response handler.

The manual check above did not interrupt gateways or inject Redis failures. These are failure scenarios identified from the current operation sequence, not failures reproduced by that check.

## Checking the infrastructure

```sh
docker compose ps
docker compose logs -f gateway user_service order_service
docker compose exec redis redis-cli ping
docker compose exec gateway python manage.py check
curl http://localhost:8000/health/
curl http://localhost:8001/health/
curl http://localhost:8002/health/
```

## Sample requests

```sh
curl "http://localhost:8000/api/users/1/"
curl "http://localhost:8000/api/orders/1/?expand=items"
curl -X POST http://localhost:8000/api/orders/ \
  -H "Content-Type: application/json" \
  -d '{"user_id": 1, "items": [{"sku": "a", "qty": 2}]}'
curl -i http://localhost:8000/api/unknown/   # 404
curl -i http://localhost:8000/api/users/slow/ # 504 after about 5 s
```

## Running the tests

The gateway has two kinds of tests:

- `core/tests_e2e.py` (`GatewayE2ETests`) sends real HTTP requests through the running gateway to user_service and order_service. It covers routing, GET query parameters, POST JSON bodies and content types, upstream status forwarding, unknown routes (404), and timeouts (504, using the user_service `/v1/users/slow/` endpoint, which waits 6 s). These tests need the Docker Compose stack to be running. They are skipped when the gateway is not reachable.
- In `core/tests.py`, `UpstreamConnectionErrorTests` mocks upstream connection errors (502). The rate-limiter tests use the real Redis at `REDIS_URL` and control the clock, so window changes can be tested without waiting 60 seconds. Redis-backed tests use random `test-` limiter names and delete only their own keys.
- `MultiGatewaySharedQuotaTests` in `core/tests.py` sends sequential HTTP requests to two **already-running** gateway instances. It does not start or stop processes or containers, patch clocks, change limits, or delete Redis data. It verifies shared GET and POST quotas and confirms that POST remains available after GET is exhausted. Set `GATEWAY_SECOND_URL` to enable it; otherwise it is skipped. If either gateway's health endpoint is unreachable or returns an error, the class is also skipped.

### Containers expected by the multi-gateway test

The test expects two distinct gateway processes running the same code and rate-limit settings, both connected to the same Redis server and database. The user and order services must also be running. Both gateways must see the test runner as the same client IP. The current configuration allows 30 GET and 10 POST requests per 60-second window.

For a test run from the host, expose the gateways at ports 8000 and 8003 and set `GATEWAY_URL=http://localhost:8000` and `GATEWAY_SECOND_URL=http://localhost:8003`. The second container is managed externally; the test never creates it. Different URLs must actually point to different gateway instances.

For example, with the usual Compose stack running, the following starts the additional container and runs just the shared-quota test from the main gateway container:

```sh
docker compose up -d --build --wait
docker compose run -d --no-deps --name gateway-quota-check -p 127.0.0.1:8003:8000 -e DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,gateway-quota-check gateway
docker compose exec -e GATEWAY_URL=http://gateway:8000 -e GATEWAY_SECOND_URL=http://gateway-quota-check:8000 gateway python manage.py test core.tests.MultiGatewaySharedQuotaTests
```

Container-based runs use Docker network names instead of the host's published ports. The extra allowed host lets the second gateway accept requests addressed to its container name. The example expects the name and port to be available. Stop and remove the additional container separately when finished.

The test waits for a new fixed window, then distributes requests between both instances. After exhausting each method's combined quota, it checks that both instances return 429 with a valid `Retry-After`. It waits for the next window before finishing so subsequent tests can use the client's allowance. With 60-second windows, this test takes roughly one to two minutes. It requires synchronized clocks, identical gateway limits, and no other traffic from the same client IP during the run; requests must complete within one window. Unlike the isolated Redis tests, it deliberately consumes the live client's quota.


The easiest way is to run them inside the gateway container:

```sh
docker compose up --build -d
docker compose exec gateway python manage.py test core
```

To run them on the host, you need Python 3.13, the packages in `requirements.txt`, and a Redis server reachable at `REDIS_URL` (default `redis://localhost:6379/0`; the Compose Redis does not publish a port to the host). The e2e tests call `http://localhost:8000` by default; set `GATEWAY_URL` to change it.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd gateway && cp .env.example .env
python manage.py test core
```

The ordinary run includes a 5-second upstream timeout test. Enabling the multi-gateway test adds roughly one to two minutes for window alignment and quota recovery. The e2e tests count toward the live rate limits; if you run them many times within a minute and get 429 responses, reset the limits as described above.

## Intentionally unsupported

- HTTP methods other than GET and POST
- Forwarding client headers other than `Content-Type`, or upstream response headers other than `Content-Type`
- Authentication, caching, retries, load balancing, and dynamic service discovery
- Streaming request and response bodies, WebSockets, and advanced HTTP edge cases
- Connection pool tuning beyond the defaults in `gateway/core/http.py`

## Environment templates

Each Django service includes an environment template:

- `gateway/.env.example`
- `user_service/.env.example`
- `order_service/.env.example`

## Package versions

The latest stable versions verified through PyPI on November 14, 2026, are pinned. Python 3.13 meets the packages' Python requirements; DRF requires Django >=5.2.

- [Django 6.1.3](https://pypi.org/project/Django/6.1.3/)
- [Django REST framework 3.18.3](https://pypi.org/project/djangorestframework/3.18.3/)
- [redis-py 8.1.0](https://pypi.org/project/redis/8.1.0/)
- [Daphne 4.2.3](https://pypi.org/project/daphne/4.2.3/)
- [HTTPX 0.28.1](https://pypi.org/project/httpx/0.28.1/)

requirements.txt pins direct dependencies; it does not lock all transitive dependencies. The redis:8-alpine and python:3.13-slim images receive updates within their respective release series.
