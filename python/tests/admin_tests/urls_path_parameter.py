"""URL configuration with a path parameter before the admin site."""

from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("<slug:tenant>/admin/", admin.site.urls),
]
