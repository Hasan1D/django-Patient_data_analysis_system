import django.db.models.deletion
from django.db import migrations, models


def merge_duplicate_medical_histories(apps, schema_editor):
    MedicalHistory = apps.get_model("core", "MedicalHistory")
    Vaccine = apps.get_model("core", "Vaccine")
    Allergy = apps.get_model("core", "Allergy")
    ChronicDisease = apps.get_model("core", "chronicDisease")
    SurgicalHistory = apps.get_model("core", "SurgicalHistory")

    patient_ids = (
        MedicalHistory.objects.values_list("patient_id", flat=True)
        .order_by("patient_id")
        .distinct()
    )

    for patient_id in patient_ids:
        histories = list(MedicalHistory.objects.filter(patient_id=patient_id).order_by("id"))
        if len(histories) <= 1:
            continue

        keeper = histories[0]
        duplicate_ids = [history.id for history in histories[1:]]
        if any(history.has_surgical_metal_plates for history in histories[1:]):
            keeper.has_surgical_metal_plates = True
            keeper.save(update_fields=["has_surgical_metal_plates"])

        Vaccine.objects.filter(history_id__in=duplicate_ids).update(history_id=keeper.id)
        Allergy.objects.filter(history_id__in=duplicate_ids).update(history_id=keeper.id)
        ChronicDisease.objects.filter(history_id__in=duplicate_ids).update(history_id=keeper.id)
        SurgicalHistory.objects.filter(history_id__in=duplicate_ids).update(history_id=keeper.id)
        MedicalHistory.objects.filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_restrict_visit_marital_status_choices"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_medical_histories, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="medicalhistory",
            name="patient",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                to="core.patient",
            ),
        ),
    ]
