from django.db import migrations, models


def normalize_visit_statuses(apps, schema_editor):
    Visit = apps.get_model("core", "Visit")
    cured_values = {"cured", "recovered", "recovery", "healed", "شفاء", "متعافي"}

    for visit in Visit.objects.all().only("id", "status"):
        normalized_status = (visit.status or "").strip().lower()
        new_status = "cured" if normalized_status in cured_values else "infected"
        if visit.status != new_status:
            Visit.objects.filter(id=visit.id).update(status=new_status)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_restrict_patient_gender_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_visit_statuses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="visit",
            name="status",
            field=models.CharField(
                choices=[("infected", "Infected"), ("cured", "Cured")],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="visit",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=["infected", "cured"]),
                name="core_visit_status_infected_or_cured",
            ),
        ),
    ]
