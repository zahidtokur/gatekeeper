from adrf.views import APIView
from rest_framework.response import Response

# Mock view
class UserRetrieve(APIView):
    async def get(self, request, pk):
        return Response({
            "service": "user-service",
            "method": request.method,
            "path": request.path,
            "query_params": request.query_params,
        })
