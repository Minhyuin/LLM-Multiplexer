from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api_keys", "0004_apikeyalias"),
    ]

    operations = [
        migrations.AddField(
            model_name="apikey",
            name="token_rotated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
