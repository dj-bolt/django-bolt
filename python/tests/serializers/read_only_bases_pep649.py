"""Serializers for the ``Config.read_only`` tests on Python 3.14 (PEP 649).

This module has no ``from __future__ import annotations`` on purpose. On
Python 3.14, the class bodies keep their annotations in ``__annotate__``.
"""

from django_bolt.serializers import Serializer


class Label(Serializer):
    text: str = ""


class LazyBase(Serializer):
    id: int = 0
    label: Label | None = None
    name: str


class LazyChild(LazyBase, forbid_unknown_fields=True):
    note: str = ""

    class Config:
        read_only = {"id", "label"}


class LazyBareChild(LazyBase, forbid_unknown_fields=True):
    class Config:
        read_only = {"id", "label"}
