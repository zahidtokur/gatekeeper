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

The following decisions define the planned rate limiter:

For local development, the gateway will use the client's resolved IP address as its identifier. Requests with different source IP addresses belong to different clients. Requests sharing an IP address, including clients behind the same NAT, share the same allowance. To test two independent clients, use two distinct source IP addresses; separate processes on the same IP do not count as separate clients.

This identifier is **not authentication** and provides no proof of identity. IP-based identification can be spoofed, particularly if untrusted forwarding headers are accepted. Do not treat an arbitrary client-supplied IP header as a verified identity.

The default policy uses a fixed 60-second window and separate counters for each client and HTTP method:

| Method | Requests allowed per client per 60-second window |
| --- | --- |
| GET | 30 |
| POST | 10 |

These limits cover all proxied routes under `/api/users/` and `/api/orders/`, including nested paths. Each client's GET allowance is shared across both services, and its POST allowance is also shared across both services. Switching services or paths does not provide a fresh allowance. GET requests do not consume the POST allowance, and vice versa. Health checks at `/health/` and non-proxy routes are outside this policy; methods other than GET and POST remain unsupported.

If the gateway cannot determine a client's IP address, it will reject the request with **503 Service Unavailable** rather than forward it or assign a shared anonymous identifier.

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
- `core/tests.py` (`UpstreamConnectionErrorTests`) mocks the upstream client to test connection errors (502). A running service cannot produce a connection error, so this test needs no Docker.

The easiest way is to run them inside the gateway container:

```sh
docker compose up --build -d
docker compose exec gateway python manage.py test core
```

To run them on the host, you need Python 3.13 and the packages in `requirements.txt`. The e2e tests call `http://localhost:8000` by default; set `GATEWAY_URL` to change it.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd gateway && cp .env.example .env
python manage.py test core
```

The full run takes about 5 seconds because of the timeout test.

## Intentionally unsupported

- HTTP methods other than GET and POST
- Forwarding client headers other than `Content-Type`, or upstream response headers other than `Content-Type`
- Authentication, caching, retries, load balancing, and dynamic service discovery
- Rate-limit enforcement (the planned policy is documented above)
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
