"""URL configuration with the admin site at the root path."""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("", admin.site.urls),
]
