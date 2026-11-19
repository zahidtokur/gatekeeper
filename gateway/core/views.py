import httpx

from adrf.views import APIView
from django.conf import settings
from django.http import Http404, HttpResponse
from rest_framework.response import Response

from core.http import get_client
from core.rate_limiter import FixedWindowRateLimiter


get_request_limiter = FixedWindowRateLimiter(name="get", limit=30, window_size=60)
post_request_limiter = FixedWindowRateLimiter(name="post", limit=10, window_size=60)


class GatewayProxy(APIView):
    http_method_names = ['get', 'post']
    services = {
        "users": settings.USER_SERVICE_URL + "/v1/users/",
        "orders": settings.ORDER_SERVICE_URL + "/v1/orders/",
    }
    rate_limiters = {
        "GET": get_request_limiter,
        "POST": post_request_limiter
    }
    
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
                {"detail": "Rate limit exceeded. Retry after the time in the Retry-After header."},
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

        upstream_url = self.build_upstream_url(service_name, path)
        headers = {}
        if ct := request.headers.get("content-type"):
            headers["content-type"] = ct
            
        return await self._forward(request, upstream_url, headers)

    async def _forward(self, request, url, headers):
        client = get_client()
        try:
            upstream_response = await client.request(
                request.method,
                url,
                content=request.body or None,
                headers=headers,
                params=dict(request.query_params.lists())
            )
        except httpx.ConnectTimeout:
            return Response(
                {"detail": "Connection timed out."},
                status=502,
            )
        except httpx.TimeoutException:
            return Response(
                {"detail": "Upstream timeout"},
                status=504
            )
        except httpx.RequestError:
            return Response(
                {"detail": "Upstream connection error"},
                status=502
            )

        return HttpResponse(
            content=upstream_response.content,
            status=upstream_response.status_code,
            content_type=upstream_response.headers.get(
                "content-type",
                "application/octet-stream"
            )
        )


    def build_upstream_url(self, service_name, path):
        upstream_url = self.services.get(service_name)
        if not upstream_url:
            raise Http404(
                f"No service named {service_name} found for path {path}"
            )

        if path and not path.endswith("/"):
            path += "/"
        upstream_url += path
        return upstream_url

    async def get(self, request, service_name, path=''):
        return await self._process(request, service_name, path)

    async def post(self, request, service_name, path=''):
        return await self._process(request, service_name, path)
