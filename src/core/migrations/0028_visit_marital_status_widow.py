from django.db import migrations, models


def forwards_widow_value(apps, schema_editor):
    Visit = apps.get_model("core", "Visit")
    Visit.objects.filter(marital_status="widow(er)").update(marital_status="widow")


def backwards_widow_value(apps, schema_editor):
    Visit = apps.get_model("core", "Visit")
    Visit.objects.filter(marital_status="widow").update(marital_status="widow(er)")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_supportticket_supportmessage"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="visit",
            name="core_visit_marital_status_valid",
        ),
        migrations.RunPython(forwards_widow_value, backwards_widow_value),
        migrations.AlterField(
            model_name="visit",
            name="marital_status",
            field=models.CharField(
                choices=[
                    ("divorced", "Divorced"),
                    ("single", "Single"),
                    ("married", "Married"),
                    ("widow", "Widow"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="visit",
            constraint=models.CheckConstraint(
                condition=models.Q(marital_status__in=["divorced", "single", "married", "widow"]),
                name="core_visit_marital_status_valid",
            ),
        ),
    ]
