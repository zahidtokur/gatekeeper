from django.urls import path
from orders import views
from rest_framework.routers import DefaultRouter

app_name = "orders"

router = DefaultRouter()
router.register(r'', views.OrderViewset, basename="order")

urlpatterns = [
    path("slow/", views.SlowResponse.as_view()),
    path("error/", views.ErrorResponse.as_view()),
] + router.urls
