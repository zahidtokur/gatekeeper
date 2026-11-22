import httpx
import asyncio

from adrf.views import APIView
from django.conf import settings
from django.http import Http404, HttpResponse
from rest_framework.response import Response

from core.circuit_breaker import CircuitBreaker
from core.clients import get_http_client
from core.rate_limiter import FixedWindowRateLimiter

get_request_limiter = FixedWindowRateLimiter(name="get", limit=settings.RATE_LIMIT_GET, window_size=60)
post_request_limiter = FixedWindowRateLimiter(name="post", limit=settings.RATE_LIMIT_POST, window_size=60)
user_service_cb = CircuitBreaker()
order_service_cb = CircuitBreaker()


class GatewayProxy(APIView):
    http_method_names = ["get", "post"]
    services = {
        "users": {
            "url": settings.USER_SERVICE_URL + "/v1/users/",
            "circuit_breaker": user_service_cb,
        },
        "orders": {
            "url": settings.ORDER_SERVICE_URL + "/v1/orders/",
            "circuit_breaker": order_service_cb,
        },
    }
    rate_limiters = {"GET": get_request_limiter, "POST": post_request_limiter}

    async def _process(self, request, service_name, path):
        ip_address = request.META.get("REMOTE_ADDR")

        if not ip_address:
            return Response(
                {"detail": "Unable to identify client"},
                status=503,
            )

        rate_limiter = self.rate_limiters.get(request.method)
        is_limited, retry_after = await rate_limiter.check(ip_address)
        if is_limited:
            return Response(
                {
                    "detail": "Rate limit exceeded. Retry after the time in the Retry-After header."
                },
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

        service = self.services.get(service_name)
        if not service:
            raise Http404(f"No service named {service_name} found for path {path}")

        upstream_url = self.build_upstream_url(service["url"], path)
        cb = service["circuit_breaker"]
        headers = {}
        if ct := request.headers.get("content-type"):
            headers["content-type"] = ct

        return await self._forward(request, cb, upstream_url, headers)

    async def _forward(self, request, cb, url, headers):
        if not settings.CIRCUIT_BREAKER_ENABLED or cb.allow_request():
            client = get_http_client()
            try:
                upstream_response = await client.request(
                    request.method,
                    url,
                    content=request.body or None,
                    headers=headers,
                    params=dict(request.query_params.lists()),
                )
            except asyncio.CancelledError:
                cb.abort_request()
                raise
            except httpx.RequestError as e:
                cb.record_failure()
                if isinstance(e, httpx.ConnectTimeout):
                    return Response(
                        {"detail": "Connection timed out."},
                        status=502,
                    )
                if isinstance(e, httpx.TimeoutException):
                    return Response({"detail": "Upstream timeout"}, status=504)

                return Response({"detail": "Upstream connection error"}, status=502)

            if upstream_response.status_code >= 500:
                cb.record_failure()
            else:
                cb.record_success()

            return HttpResponse(
                content=upstream_response.content,
                status=upstream_response.status_code,
                content_type=upstream_response.headers.get(
                    "content-type", "application/octet-stream"
                ),
            )

        return Response({"detail": "Service temporarily unavailable."}, status=503)

    def build_upstream_url(self, url, path):
        if path and not path.endswith("/"):
            path += "/"
        return url + path

    async def get(self, request, service_name, path=""):
        return await self._process(request, service_name, path)

    async def post(self, request, service_name, path=""):
        return await self._process(request, service_name, path)
