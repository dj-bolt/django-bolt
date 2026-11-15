"""URL configuration with the admin site in ``i18n_patterns()``, with no prefix for the default language."""

from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import path

urlpatterns = i18n_patterns(path("admin/", admin.site.urls), prefix_default_language=False)
