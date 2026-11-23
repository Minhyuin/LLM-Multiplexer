from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_normalize_orphan_lab_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="daily_token_quota",
            field=models.PositiveBigIntegerField(
                blank=True,
                help_text="单位 token；留空时使用站点设置里的成员默认额度。",
                null=True,
                verbose_name="每日 Token 额度",
            ),
        ),
    ]
