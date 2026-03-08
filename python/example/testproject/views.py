import asyncio
import time

import test_data
from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse


async def index(request):
    return JsonResponse(test_data.JSON_1K, safe=False)


async def sse(request):
    """Server-Sent Events endpoint that sends timestamp data every second."""

    async def gen():
        while True:
            await asyncio.sleep(1)
            yield f"data: {time.time()}\n\n"

    return StreamingHttpResponse(gen(), content_type="text/event-stream")


def accounts_index(request):
    """Mounted Django accounts testing index."""
    base = reverse("accounts-index").rstrip("/")
    return JsonResponse(
        {
            "source": "django_mount",
            "message": "Mounted Django accounts test endpoints",
            "endpoints": {
                "login": reverse("accounts-login"),
                "logout": reverse("accounts-logout"),
                "profile": reverse("accounts-profile"),
                "provider_callback": f"{reverse('accounts-provider-callback')}?code=demo&state=xyz",
                "django_auth_urls": f"{base}/auth/",
                "allauth_if_installed": f"{base}/allauth/",
            },
        }
    )


def accounts_login(request):
    """
    Minimal login endpoint for mounted-Django testing.

    POST form fields:
    - username
    - password
    - next (optional, default: /accounts/profile/)
    """
    if request.method == "GET":
        return JsonResponse(
            {
                "source": "django_mount",
                "action": "login",
                "method": "POST",
                "required_fields": ["username", "password"],
                "optional_fields": ["next"],
            }
        )

    username = request.POST.get("username") or request.GET.get("username")
    password = request.POST.get("password") or request.GET.get("password")
    next_url = request.POST.get("next") or request.GET.get("next") or reverse("accounts-profile")

    if not username or not password:
        return JsonResponse(
            {"ok": False, "error": "username and password are required"},
            status=400,
        )

    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({"ok": False, "error": "invalid credentials"}, status=401)

    login(request, user)
    return redirect(next_url)


def accounts_logout(request):
    """Logout endpoint for mounted-Django testing."""
    logout(request)
    return redirect(reverse("accounts-login"))


def accounts_profile(request):
    """Simple auth state endpoint for mounted-Django testing."""
    user = request.user
    is_authenticated = bool(getattr(user, "is_authenticated", False))
    return JsonResponse(
        {
            "source": "django_mount",
            "authenticated": is_authenticated,
            "username": user.get_username() if is_authenticated else None,
        }
    )


def accounts_provider_callback(request):
    """
    Simulates an OAuth provider callback endpoint (allauth-style URL shape).

    Example:
    /accounts/provider/callback/?code=demo-code&state=demo-state
    """
    return JsonResponse(
        {
            "source": "django_mount",
            "action": "provider_callback",
            "query": dict(request.GET.items()),
        }
    )
