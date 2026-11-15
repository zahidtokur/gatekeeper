from adrf.viewsets import ViewSet
from rest_framework.response import Response

# Mock viewset
class OrderViewset(ViewSet):
    async def retrieve(self, request, pk):
        return Response({
            "service": "order-service",
            "method": request.method,
            "path": request.path,
            "query_params": request.query_params,
        })

    async def create(self, request):
        return Response({
            "service": "order-service",
            "method": request.method,
            "path": request.path,
            "body": request.data,
            "query_params": request.query_params,
        })
