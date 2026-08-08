from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("bolt_mcp_oauth", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="oauthclient",
            name="application_type",
            field=models.CharField(default="web", max_length=16),
        ),
    ]
