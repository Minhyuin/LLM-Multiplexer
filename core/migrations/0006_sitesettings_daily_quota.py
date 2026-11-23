import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_sitesettings_feature_controls"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="daily_token_quota_default",
            field=models.PositiveBigIntegerField(
                default=200000000,
                help_text="单位 token（200000000 = 200M）。统一设定时会覆盖所有成员的额度。",
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="成员每日 Token 额度",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="quota_enforcement",
            field=models.CharField(
                choices=[
                    ("block", "停用该成员密钥"),
                    ("warn", "仅提示不阻断"),
                    ("off", "关闭额度检查"),
                ],
                default="block",
                help_text="停用密钥后在内核侧拒绝请求，次日 0 点或调高额度后自动恢复。",
                max_length=8,
                verbose_name="每日额度超额处理",
            ),
        ),
    ]
