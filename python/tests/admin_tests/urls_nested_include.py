"""URL configuration with the admin site inside two nested includes."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("portal/", include([path("internal/", include([path("staff/", admin.site.urls)]))])),
]
