from django.db import migrations, models


def normalize_patient_genders(apps, schema_editor):
    Patient = apps.get_model("core", "Patient")
    male_values = {"male", "m", "ذكر"}
    female_values = {"female", "f", "أنثى", "انثى"}

    for patient in Patient.objects.all().only("id", "gender"):
        normalized_gender = (patient.gender or "").strip().lower()
        if normalized_gender in female_values:
            new_gender = "female"
        elif normalized_gender in male_values:
            new_gender = "male"
        else:
            new_gender = "male"

        if patient.gender != new_gender:
            Patient.objects.filter(id=patient.id).update(gender=new_gender)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_restrict_user_role_choices"),
    ]

    operations = [
        migrations.RunPython(normalize_patient_genders, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="patient",
            name="gender",
            field=models.CharField(
                choices=[("male", "Male"), ("female", "Female")],
                max_length=10,
            ),
        ),
        migrations.AddConstraint(
            model_name="patient",
            constraint=models.CheckConstraint(
                condition=models.Q(gender__in=["male", "female"]),
                name="core_patient_gender_male_or_female",
            ),
        ),
    ]
