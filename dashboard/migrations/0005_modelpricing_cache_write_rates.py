from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_merge_20260510_1050"),
    ]

    operations = [
        migrations.AddField(
            model_name="modelpricing",
            name="cache_write_price_per_million",
            field=models.DecimalField(decimal_places=6, default=0, max_digits=12),
        ),
        migrations.AddField(
            model_name="modelpricing",
            name="cache_write_1h_price_per_million",
            field=models.DecimalField(decimal_places=6, default=0, max_digits=12),
        ),
    ]
