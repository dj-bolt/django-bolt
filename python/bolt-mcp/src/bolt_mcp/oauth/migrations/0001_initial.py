from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="OAuthClient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("client_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("client_name", models.CharField(blank=True, default="", max_length=255)),
                ("redirect_uris", models.JSONField(default=list)),
                ("grant_types", models.JSONField(default=list)),
                ("scope", models.TextField(blank=True, default="")),
                ("token_endpoint_auth_method", models.CharField(default="none", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="AuthorizationCode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("user_id", models.CharField(max_length=255)),
                ("redirect_uri", models.TextField()),
                ("code_challenge", models.CharField(max_length=255)),
                ("code_challenge_method", models.CharField(default="S256", max_length=10)),
                ("scope", models.TextField(blank=True, default="")),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="codes",
                        to="bolt_mcp_oauth.oauthclient",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RefreshToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(db_index=True, max_length=64, unique=True)),
                ("chain_id", models.CharField(db_index=True, max_length=64)),
                ("user_id", models.CharField(max_length=255)),
                ("scope", models.TextField(blank=True, default="")),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("rotated", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refresh_tokens",
                        to="bolt_mcp_oauth.oauthclient",
                    ),
                ),
            ],
        ),
    ]
