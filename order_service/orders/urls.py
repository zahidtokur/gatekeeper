from orders import views
from rest_framework.routers import DefaultRouter

app_name = "orders"

router = DefaultRouter()
router.register(r'', views.OrderViewset, basename="order")
urlpatterns = router.urls
