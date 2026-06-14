import contextlib

from django.urls import include, path

from . import views

urlpatterns = [
    path("", views.accounts_index, name="accounts-index"),
    path("login/", views.accounts_login, name="accounts-login"),
    path("logout/", views.accounts_logout, name="accounts-logout"),
    path("profile/", views.accounts_profile, name="accounts-profile"),
    path(
        "provider/callback/",
        views.accounts_provider_callback,
        name="accounts-provider-callback",
    ),
    # Django built-in auth URL set (login/logout/password reset views)
    path("auth/", include("django.contrib.auth.urls")),
]

# Optional allauth URLs for mounted-Django integration testing.
# If allauth is not installed/configured, skip silently.
with contextlib.suppress(Exception):
    urlpatterns.append(path("allauth/", include("allauth.urls")))
