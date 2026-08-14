from django.db import migrations, models


def normalize_visit_marital_statuses(apps, schema_editor):
    Visit = apps.get_model("core", "Visit")
    value_map = {
        "divorced": "divorced",
        "single": "single",
        "married": "married",
        "widow(er)": "widow(er)",
        "widow": "widow(er)",
        "widower": "widow(er)",
        "widowed": "widow(er)",
        "مطلق": "divorced",
        "مطلقة": "divorced",
        "اعزب": "single",
        "عازب": "single",
        "عزباء": "single",
        "متزوج": "married",
        "متزوجة": "married",
        "ارمل": "widow(er)",
        "أرمل": "widow(er)",
        "ارملة": "widow(er)",
        "أرملة": "widow(er)",
    }

    for visit in Visit.objects.all().only("id", "marital_status"):
        normalized_status = (visit.marital_status or "").strip().lower()
        new_status = value_map.get(normalized_status, "single")
        if visit.marital_status != new_status:
            Visit.objects.filter(id=visit.id).update(marital_status=new_status)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_restrict_geodata_region_type_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_visit_marital_statuses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="visit",
            name="marital_status",
            field=models.CharField(
                choices=[
                    ("divorced", "Divorced"),
                    ("single", "Single"),
                    ("married", "Married"),
                    ("widow(er)", "Widow(er)"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="visit",
            constraint=models.CheckConstraint(
                condition=models.Q(marital_status__in=["divorced", "single", "married", "widow(er)"]),
                name="core_visit_marital_status_valid",
            ),
        ),
    ]
