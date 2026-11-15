"""URL configuration with the admin site behind a regex pattern."""

from django.contrib import admin
from django.urls import re_path

urlpatterns = [
    re_path(r"^manage/", admin.site.urls),
]
