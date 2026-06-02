from django.apps import AppConfig


class DjangoBoltConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_bolt"
    verbose_name = "Django Bolt"

    def ready(self):
        # Install django_bolt.urls.reverse patch.
        from .urls import patch_django_reverse

        patch_django_reverse()

