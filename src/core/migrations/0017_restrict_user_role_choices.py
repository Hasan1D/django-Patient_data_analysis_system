from django.db import migrations, models


def normalize_user_roles(apps, schema_editor):
    User = apps.get_model("core", "User")
    User.objects.exclude(role__in=["admin", "doctor"]).update(role="doctor")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_visit_diagnose"),
    ]

    operations = [
        migrations.RunPython(normalize_user_roles, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("admin", "Admin"), ("doctor", "Doctor")],
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(role__in=["admin", "doctor"]),
                name="core_user_role_admin_or_doctor",
            ),
        ),
    ]
