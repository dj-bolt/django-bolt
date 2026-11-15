"""URL configuration with an include from an "admin" module before the admin site.

The name of this module contains "admin", and the include comes first.
"""

from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path


def report(request):
    return HttpResponse("report")


urlpatterns = [
    path("reports/", include([path("", report)])),
    path("backoffice/", admin.site.urls),
]
