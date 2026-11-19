import asyncio

from adrf.views import APIView
from adrf.viewsets import ViewSet
from rest_framework import status
from rest_framework.response import Response

# Mock viewset
class OrderViewset(ViewSet):
    async def retrieve(self, request, pk):
        return Response({
            "service": "order-service",
            "method": request.method,
            "path": request.path,
            "query_params": dict(request.query_params),
        })

    async def create(self, request):
        return Response({
            "service": "order-service",
            "method": request.method,
            "path": request.path,
            "body": request.data,
            "query_params": dict(request.query_params),
        })


class SlowResponse(APIView):
    async def get(self, request):
        try:
            seconds = float(request.query_params.get("seconds", "0"))
        except ValueError:
            return Response({"detail": "seconds must be a number"},
                            status=status.HTTP_400_BAD_REQUEST)

        if seconds < 0 or seconds > 30:
            return Response({"detail": "Invalid time params"},
                            status=status.HTTP_400_BAD_REQUEST)
        await asyncio.sleep(seconds)
        return Response({"service": "order-service", "slow": True,
                         "delayed_seconds": seconds})



class ErrorResponse(APIView):
    async def get(self, request):
        error_code = request.query_params.get("error_code")

        if error_code not in ("500", "503"):
            return Response({"detail": "error_code must be 500 or 503"},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"service": "order-service", "error": True},
                        status=int(error_code))
