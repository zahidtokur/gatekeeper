# Gatekeeper

Three independent Django projects for learning how to build an API gateway: gateway, user_service, and order_service. The stack uses Python 3.13 and Daphne/ASGI. A shared Dockerfile builds a separate image for each service.

## Running the project

Docker Desktop must be running with Linux containers enabled. Docker Compose v2 is required. From the project root:

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f
```

On startup, each Django service runs migrations against its own database, then starts Daphne. The gateway waits for the user and order services and Redis to pass their health checks. Continue to handle connection failures and timeouts in your gateway code, since upstream services can become unavailable later.

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
