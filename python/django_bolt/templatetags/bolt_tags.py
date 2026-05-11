"""Django template tags for Django-Bolt URL reversing."""

from django import template

from django_bolt.urls import reverse

register = template.Library()


@register.simple_tag
def bolt_url(name: str, **kwargs) -> str:
    """Resolve a Bolt route name + params to a URL string."""
    return reverse(name, **kwargs)
