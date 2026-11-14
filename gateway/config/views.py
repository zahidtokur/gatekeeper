import asyncio

import httpx
from django.conf import settings
from django.http import JsonResponse


async def health(request):
    return JsonResponse({"status": "ok", "service": "gateway"})


async def overview(request):
    if request.method != "GET":
        response = JsonResponse({"detail": "Method not allowed"}, status=405)
        response["Allow"] = "GET"
        return response
    
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0), trust_env=False) as client:
        try:
            users, orders = await asyncio.gather(
                client.get(f"{settings.USER_SERVICE_URL.rstrip('/')}/api/users/"),
                client.get(f"{settings.ORDER_SERVICE_URL.rstrip('/')}/api/orders/"),
            )
            users.raise_for_status()
            orders.raise_for_status()
            return JsonResponse({"users": users.json()["users"], "orders": orders.json()["orders"]})
        except httpx.TimeoutException:
            return JsonResponse({"detail": "Upstream timeout"}, status=504)
        except (httpx.HTTPError, ValueError, KeyError):
            return JsonResponse({"detail": "Upstream unavailable or invalid response"}, status=502)
