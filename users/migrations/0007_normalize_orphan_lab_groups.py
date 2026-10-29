from django.db import migrations

FALLBACK_GROUPS = ("电控", "算法", "机械", "宣传")
UNASSIGNED = "未分组"


def normalize_orphan_groups(apps, schema_editor):
    Profile = apps.get_model("users", "Profile")
    SiteSettings = apps.get_model("core", "SiteSettings")
    raw = ""
    if SiteSettings.objects.filter(pk=1).exists():
        raw = SiteSettings.objects.filter(pk=1).values_list("lab_groups", flat=True).first() or ""
    configured = [line.strip() for line in raw.splitlines() if line.strip()]
    valid = set(configured or FALLBACK_GROUPS)
    Profile.objects.exclude(lab_group__in=valid).exclude(lab_group=UNASSIGNED).update(
        lab_group=UNASSIGNED
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_sitesettings_feature_controls"),
        ("users", "0006_alter_profile_lab_group"),
    ]

    operations = [
        migrations.RunPython(normalize_orphan_groups, migrations.RunPython.noop),
    ]
