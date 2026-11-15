"""URL configuration with a custom AdminSite that has its own name."""

from django.contrib.admin import AdminSite
from django.urls import path

site = AdminSite(name="backoffice")

urlpatterns = [
    path("office/", site.urls),
]
