"""
URL configuration for testproject project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path

from . import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("testproject.mount_urls")),
    # Register a reverse-only entry for every named Bolt route, so Django's
    # reverse()/{% url %} can resolve them even though they're served in Rust.
    # See missions/api.py (reverse_demo) and missions/dashboard.html.
    path("", include("django_bolt.urls")),
    path("", views.index, name="index"),
    path("sse", views.sse, name="sse"),
]
