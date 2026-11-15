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
- An unknown service returns `404`. An upstream connection error returns `502`, and an upstream timeout returns `504` (5 s total, 2 s connect).

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
```

## Running the tests

The gateway tests need no Docker, database, or running services. They need Python 3.13 and the packages in `requirements.txt`:

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd gateway && cp .env.example .env
python manage.py test core
```

With Docker: `docker compose exec gateway python manage.py test core`

- `GatewayProxyTests` mocks the upstream client and covers routing, GET query parameters, POST bodies and content types, status and body forwarding, 404, 502, and 504.
- `GatewaySmokeTests` starts a real dummy HTTP upstream on a free local port and sends a real GET and POST through the gateway to it.

## Intentionally unsupported

- HTTP methods other than GET and POST
- Forwarding client headers other than `Content-Type`, or upstream response headers other than `Content-Type`
- Authentication, rate limiting, caching, retries, load balancing, and dynamic service discovery
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
