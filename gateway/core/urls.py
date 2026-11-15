from django.urls import re_path
from . import views

app_name = "core"

urlpatterns = [
    re_path(r'^api/(?P<service_name>[^/]+)/(?P<path>.*)$', views.GatewayProxy.as_view(), name='gateway'),
]
