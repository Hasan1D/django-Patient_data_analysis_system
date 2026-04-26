from django.db import migrations, models


def normalize_geodata_region_types(apps, schema_editor):
    GeoData = apps.get_model("core", "GeoData")
    work_values = {"work", "office", "job", "عمل"}

    for geodata in GeoData.objects.all().only("id", "region_type"):
        normalized_region_type = (geodata.region_type or "").strip().lower()
        new_region_type = "work" if normalized_region_type in work_values else "home"
        if geodata.region_type != new_region_type:
            GeoData.objects.filter(id=geodata.id).update(region_type=new_region_type)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_restrict_visit_status_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_geodata_region_types, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="geodata",
            name="region_type",
            field=models.CharField(
                choices=[("home", "Home"), ("work", "Work")],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="geodata",
            constraint=models.CheckConstraint(
                condition=models.Q(region_type__in=["home", "work"]),
                name="core_geodata_region_type_home_or_work",
            ),
        ),
    ]
