import asyncio

from adrf.views import APIView
from rest_framework.response import Response

# Mock view
class UserRetrieve(APIView):
    async def get(self, request, pk):
        return Response({
            "service": "user-service",
            "method": request.method,
            "path": request.path,
            "query_params": dict(request.query_params),
        })


# Responds slower than the gateway timeout (5s) so the gateway returns 504.
class SlowResponse(APIView):
    async def get(self, request):
        await asyncio.sleep(6)
        return Response({"service": "user-service", "slow": True})
