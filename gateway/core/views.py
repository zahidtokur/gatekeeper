import httpx

from adrf.views import APIView
from django.conf import settings
from django.http import HttpResponse
from rest_framework.response import Response

from core.http import get_client


class GatewayProxy(APIView):
    services = {
        "users": settings.USER_SERVICE_URL + "/v1/users/",
        "orders": settings.ORDER_SERVICE_URL + "/v1/orders/",
    }
            
    async def get(self, request, service_name, path=''):
        service_url = self.services.get(service_name)
        if not service_url:
            return Response(data={"detail": "Service not found"}, status=404)
        
        if path and not path.endswith("/"):
            path += "/"
        service_url += path
        
        client = get_client()
        try:
            upstream_response = await client.get(
                service_url,
                params=dict(request.query_params)
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
