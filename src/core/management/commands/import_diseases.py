import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import Disease
from core.services.disease_reference import REFERENCE_SOURCE_NAME, classify_reference_disease


class Command(BaseCommand):
    help = "Import reference diseases from a semicolon-delimited CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-path",
            required=True,
            help="Absolute path to the semicolon-delimited disease CSV file.",
        )
        parser.add_argument(
            "--source-name",
            default=REFERENCE_SOURCE_NAME,
            help="Reference source label stored on imported diseases.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_path"])
        source_name = options["source_name"].strip() or REFERENCE_SOURCE_NAME

        if not csv_path.exists():
            raise CommandError(f"CSV file does not exist: {csv_path}")
        if csv_path.suffix.lower() != ".csv":
            raise CommandError("CSV file path must point to a .csv file.")

        created_count = 0
        updated_count = 0

        with csv_path.open(encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file, delimiter=";")
            required_headers = {
                "id",
                "disease_code",
                "name",
                "type",
                "transmission_vector",
                "symptoms",
                "risk_level",
                "infection_score",
            }
            missing_headers = required_headers.difference(reader.fieldnames or [])
            if missing_headers:
                raise CommandError(
                    f"CSV file is missing required columns: {', '.join(sorted(missing_headers))}"
                )

            for row_number, row in enumerate(reader, start=2):
                disease_code = (row.get("disease_code") or "").strip().upper()
                name = (row.get("name") or "").strip()
                disease_type = (row.get("type") or "").strip()
                transmission_vector = (row.get("transmission_vector") or "").strip()
                symptoms = (row.get("symptoms") or "").strip()

                if not disease_code or not name:
                    raise CommandError(
                        f"Row {row_number} must include disease_code and name."
                    )

                try:
                    risk_level = int(row.get("risk_level") or 0)
                    infection_score = float(row.get("infection_score") or 0)
                    source_record_id = int(row.get("id") or 0)
                except ValueError as exc:
                    raise CommandError(f"Row {row_number} contains invalid numeric data: {exc}") from exc

                classification = classify_reference_disease(
                    disease_code=disease_code,
                    name=name,
                    disease_type=disease_type,
                    transmission_vector=transmission_vector,
                    risk_level=risk_level,
                    infection_score=infection_score,
                )
                classification["source_record_id"] = source_record_id
                classification["source_name"] = source_name

                _, created = Disease.objects.update_or_create(
                    disease_code=disease_code,
                    defaults={
                        "name": name,
                        "type": disease_type,
                        "transmission_vector": transmission_vector,
                        "symptoms": symptoms,
                        "risk_level": risk_level,
                        "infection_score": infection_score,
                        **classification,
                    },
                )
                if created:
                    created_count += 1
                else:
                    updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported diseases successfully. Created: {created_count}, Updated: {updated_count}"
            )
        )
