"""URL configuration with the admin site and a page that only Django knows."""

from django.contrib import admin
from django.http import HttpResponse
from django.urls import path


def site_page(request):
    return HttpResponse("a page of Django")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("site/", site_page),
]
