import httpx

from adrf.views import APIView
from django.conf import settings
from django.http import Http404, HttpResponse
from rest_framework.response import Response

from core.http import get_client


class GatewayProxy(APIView):
    services = {
        "users": settings.USER_SERVICE_URL + "/v1/users/",
        "orders": settings.ORDER_SERVICE_URL + "/v1/orders/",
    }

    async def _forward(self, request, service_name, path):
        service_url = self.get_service_url(service_name, path)
        headers = {}
        if ct := request.headers.get("content-type"):
            headers["content-type"] = ct

        client = get_client()
        try:
            upstream_response = await client.request(
                request.method,
                service_url,
                content=request.body or None,
                headers=headers,
                params=dict(request.query_params.lists())
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


    def get_service_url(self, service_name, path):
        service_url = self.services.get(service_name)
        if not service_url:
            raise Http404(
                f"No service named {service_name} found for path {path}"
            )

        if path and not path.endswith("/"):
            path += "/"
        service_url += path
        return service_url

    async def get(self, request, service_name, path=''):
        return await self._forward(request, service_name, path)

    async def post(self, request, service_name, path=''):
        return await self._forward(request, service_name, path)
