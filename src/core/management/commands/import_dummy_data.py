import csv
import random
import time
from datetime import date
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction
from django.db.models import Max

from core.models import Disease, Doctor, GeoCluster, GeoData, Hospital, Patient, Report, Visit
from core.services.monitoring_service import run_monitoring_for_geodata
from core.services.workflow_service import create_visit_for_patient, ensure_medical_history


User = get_user_model()


class Command(BaseCommand):
    help = "Import dummy CSV data with dependency checks and live progress tracking."

    default_filenames = {
        "hospitals": "syria_hospitals_hama_homs.csv",
        "users": "users_500.csv",
        "doctors": "doctors_456.csv",
        "patients": "patients_1000.csv",
        "visits": "visits_1500_marital_status_en.csv",
    }

    def add_arguments(self, parser):
        parser.add_argument("--data-dir", type=str, help="Directory containing the CSV files.")
        parser.add_argument("--hospitals", type=str, help="Path to syria_hospitals_hama_homs.csv")
        parser.add_argument("--users", type=str, help="Path to users_500.csv")
        parser.add_argument("--doctors", type=str, help="Path to doctors_456.csv")
        parser.add_argument("--patients", type=str, help="Path to patients_1000.csv")
        parser.add_argument("--visits", type=str, help="Path to visits_1500_marital_status_en.csv")
        parser.add_argument("--fallback-hospital-min", type=int, default=1)
        parser.add_argument("--fallback-hospital-max", type=int, default=65)
        parser.add_argument(
            "--only",
            action="append",
            choices=tuple(self.default_filenames),
            help="Import only one group. Can be passed multiple times.",
        )
        parser.add_argument("--delay-seconds", type=float, default=0.0)
        parser.add_argument("--progress-every", type=int, default=100)
        parser.add_argument("--verbose-created", action="store_true")
        parser.add_argument("--stop-after", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--replay-existing-visits",
            action="store_true",
            help="Run monitoring for existing visit rows instead of only skipping them.",
        )

    def handle(self, *args, **options):
        paths = self._resolve_paths(options)
        selected_groups = set(options["only"] or [key for key, path in paths.items() if path])
        run_options = {
            "delay_seconds": max(options["delay_seconds"], 0.0),
            "progress_every": options["progress_every"],
            "verbose_created": options["verbose_created"],
            "stop_after": options["stop_after"],
            "dry_run": options["dry_run"],
        }

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN: no database writes will be performed."))

        if paths["hospitals"] and "hospitals" in selected_groups:
            self.import_hospitals(paths["hospitals"], **run_options)
        if paths["users"] and "users" in selected_groups:
            self.import_users(paths["users"], **run_options)
        if paths["doctors"] and "doctors" in selected_groups:
            self.import_doctors(
                paths["doctors"],
                fallback_min=options["fallback_hospital_min"],
                fallback_max=options["fallback_hospital_max"],
                **run_options,
            )
        if paths["patients"] and "patients" in selected_groups:
            self.import_patients(paths["patients"], **run_options)
        if paths["visits"] and "visits" in selected_groups:
            self.import_visits(
                paths["visits"],
                replay_existing_visits=options["replay_existing_visits"],
                **run_options,
            )

        if not options["dry_run"]:
            self._reset_sequences()
        self.stdout.write(self.style.SUCCESS("\nAll requested imports are completed."))

    def _resolve_paths(self, options):
        data_dir = Path(options["data_dir"]) if options.get("data_dir") else None
        paths = {}
        for key, filename in self.default_filenames.items():
            explicit_path = options.get(key)
            if explicit_path:
                paths[key] = Path(explicit_path)
            elif data_dir:
                paths[key] = data_dir / filename
            else:
                paths[key] = None

            if paths[key] is not None and not paths[key].exists():
                raise CommandError(f"{key} CSV file does not exist: {paths[key]}")
        return paths

    def _parse_float(self, value):
        return float(value) if value and str(value).strip() else None

    def _parse_date(self, value):
        cleaned_value = self._clean_text(value)
        if not cleaned_value:
            raise ValueError("date value cannot be blank.")
        return date.fromisoformat(cleaned_value)

    def _clean_text(self, value, *, max_length=None):
        cleaned_value = (value or "").strip()
        if max_length is not None:
            return cleaned_value[:max_length]
        return cleaned_value

    def _should_report_progress(self, count, *, progress_every):
        return progress_every is not None and progress_every > 0 and count % progress_every == 0

    def _sleep_if_needed(self, *, delay_seconds, dry_run):
        if dry_run or delay_seconds <= 0:
            return
        time.sleep(delay_seconds)

    def _max_id(self, model):
        return model.objects.aggregate(max_id=Max("id"))["max_id"] or 0

    def _monitoring_snapshot(self):
        return {
            "report_max_id": self._max_id(Report),
            "cluster_max_id": self._max_id(GeoCluster),
        }

    def _monitoring_delta(self, snapshot):
        return {
            "new_report_ids": list(
                Report.objects.filter(id__gt=snapshot["report_max_id"])
                .order_by("id")
                .values_list("id", flat=True)
            ),
            "new_cluster_ids": list(
                GeoCluster.objects.filter(id__gt=snapshot["cluster_max_id"])
                .order_by("id")
                .values_list("id", flat=True)
            ),
            "report_total": Report.objects.count(),
            "cluster_total": GeoCluster.objects.count(),
        }

    def _visit_geodata_summary(self, visit):
        geodata = list(
            GeoData.objects.filter(visit=visit)
            .order_by("id")
            .values("id", "region_type", "latitude", "longitude")
        )
        region_tokens = [f"{item['region_type']}#{item['id']}" for item in geodata]
        return {
            "count": len(geodata),
            "regions": ", ".join(region_tokens) if region_tokens else "none",
        }

    def _format_visit_live_summary(self, *, visit, geodata_summary, monitoring_delta, source):
        report_ids = monitoring_delta["new_report_ids"] or "-"
        cluster_ids = monitoring_delta["new_cluster_ids"] or "-"
        return (
            f"{source} visit {visit.id}: patient={visit.patient_id}, doctor={visit.doctor_id}, "
            f"disease={visit.disease_id}, status={visit.status}, marital_status={visit.marital_status}, "
            f"GeoData={geodata_summary['count']} [{geodata_summary['regions']}], "
            f"new_reports={report_ids}, new_clusters={cluster_ids}, "
            f"report_total={monitoring_delta['report_total']}, "
            f"cluster_total={monitoring_delta['cluster_total']}"
        )

    def _fallback_hospital_id(self, *, minimum, maximum):
        hospital_ids = list(
            Hospital.objects.filter(id__gte=minimum, id__lte=maximum).values_list("id", flat=True)
        )
        if not hospital_ids:
            hospital_ids = list(Hospital.objects.values_list("id", flat=True))
        if not hospital_ids:
            raise CommandError("No hospitals exist. Import hospitals before doctors.")
        return random.choice(hospital_ids)

    def _reset_sequences(self):
        models = [Hospital, User, Doctor, Patient, Visit]
        sql_statements = connection.ops.sequence_reset_sql(no_style(), models)
        if not sql_statements:
            return
        with connection.cursor() as cursor:
            for sql in sql_statements:
                cursor.execute(sql)

    def import_hospitals(
        self,
        file_path,
        *,
        delay_seconds,
        progress_every,
        verbose_created,
        stop_after,
        dry_run,
    ):
        self.stdout.write(self.style.NOTICE(f"\n--- Importing Hospitals from {file_path} ---"))
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            count = 0
            for row_index, row in enumerate(reader, start=1):
                if stop_after is not None and row_index > stop_after:
                    break
                try:
                    defaults = {
                        "name": self._clean_text(row.get("name") or f"Hospital {row_index}", max_length=255),
                        "location": self._clean_text(row.get("location"), max_length=100),
                        "hospital_lat": self._parse_float(row.get("latitude")),
                        "hospital_long": self._parse_float(row.get("longitude")),
                        "city": self._clean_text(row.get("city"), max_length=100),
                    }
                    action = "would import"
                    if not dry_run:
                        _, created = Hospital.objects.update_or_create(id=row_index, defaults=defaults)
                        action = "created" if created else "updated"
                    count += 1
                    if verbose_created:
                        self.stdout.write(f"{action.title()} hospital {row_index}: {defaults['name']}")
                    if self._should_report_progress(count, progress_every=progress_every):
                        self.stdout.write(f"Processed {count} hospitals...")
                    self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"Hospital row {row_index} failed: {exc}"))
        verb = "Would import" if dry_run else "Imported"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} hospitals."))

    def import_users(
        self,
        file_path,
        *,
        delay_seconds,
        progress_every,
        verbose_created,
        stop_after,
        dry_run,
    ):
        self.stdout.write(self.style.NOTICE(f"\n--- Importing Users from {file_path} ---"))
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            count = 0
            for row_number, row in enumerate(reader, start=1):
                if stop_after is not None and row_number > stop_after:
                    break
                try:
                    user_id = int(row["id"])
                    defaults = {
                        "username": self._clean_text(row["username"], max_length=150),
                        "email": self._clean_text(row.get("email"), max_length=254),
                        "first_name": self._clean_text(row.get("first_name"), max_length=150),
                        "last_name": self._clean_text(row.get("last_name"), max_length=150),
                        "real_name": self._clean_text(row.get("real_name"), max_length=255),
                        "phon_number": self._clean_text(row.get("phon_number"), max_length=20),
                        "role": self._clean_text(row.get("role") or User.ROLE_DOCTOR, max_length=20),
                        "is_active": True,
                        "email_verified": True,
                        "admin_approved": True,
                    }
                    action = "would import"
                    if not dry_run:
                        user, created = User.objects.update_or_create(id=user_id, defaults=defaults)
                        if created or not user.has_usable_password():
                            user.set_password(row["password"])
                            user.save(update_fields=["password"])
                        action = "created" if created else "updated"
                    count += 1
                    if verbose_created:
                        self.stdout.write(
                            f"{action.title()} user {user_id}: {defaults['username']} ({defaults['role']})"
                        )
                    if self._should_report_progress(count, progress_every=progress_every):
                        self.stdout.write(f"Processed {count} users...")
                    self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"User row {row.get('id')} failed: {exc}"))
        verb = "Would import" if dry_run else "Imported"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} users."))

    def import_doctors(
        self,
        file_path,
        *,
        fallback_min,
        fallback_max,
        delay_seconds,
        progress_every,
        verbose_created,
        stop_after,
        dry_run,
    ):
        self.stdout.write(self.style.NOTICE(f"\n--- Importing Doctors from {file_path} ---"))
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            count = 0
            for row_number, row in enumerate(reader, start=1):
                if stop_after is not None and row_number > stop_after:
                    break
                try:
                    doctor_id = int(row["id"])
                    hospital_id = int(row["hospital_id"])
                    if not Hospital.objects.filter(id=hospital_id).exists():
                        if dry_run:
                            replacement_id = random.randint(fallback_min, fallback_max)
                        else:
                            replacement_id = self._fallback_hospital_id(
                                minimum=fallback_min,
                                maximum=fallback_max,
                            )
                        self.stdout.write(
                            self.style.WARNING(
                                f"Doctor row {row.get('id')} uses missing hospital {hospital_id}; "
                                f"using hospital {replacement_id} instead."
                            )
                        )
                        hospital_id = replacement_id

                    defaults = {
                        "user_id": int(row["user_id"]),
                        "specialization": self._clean_text(row.get("specialization"), max_length=255),
                        "hospital_id": hospital_id,
                    }
                    action = "would import"
                    if not dry_run:
                        _, created = Doctor.objects.update_or_create(id=doctor_id, defaults=defaults)
                        action = "created" if created else "updated"
                    count += 1
                    if verbose_created:
                        self.stdout.write(
                            f"{action.title()} doctor {doctor_id}: user={defaults['user_id']}, "
                            f"specialization={defaults['specialization']}, hospital={hospital_id}"
                        )
                    if self._should_report_progress(count, progress_every=progress_every):
                        self.stdout.write(f"Processed {count} doctors...")
                    self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"Doctor row {row.get('id')} failed: {exc}"))
        verb = "Would import" if dry_run else "Imported"
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} doctors."))

    def import_patients(
        self,
        file_path,
        *,
        delay_seconds,
        progress_every,
        verbose_created,
        stop_after,
        dry_run,
    ):
        self.stdout.write(self.style.NOTICE(f"\n--- Importing Patients from {file_path} ---"))
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            count = 0
            for row_number, row in enumerate(reader, start=1):
                if stop_after is not None and row_number > stop_after:
                    break
                try:
                    patient_id = int(row["id"])
                    defaults = {
                        "national_number": self._clean_text(row["national_number"], max_length=20),
                        "name": self._clean_text(row["name"], max_length=255),
                        "birth_date": row["birth_date"],
                        "gender": self._clean_text(row["gender"]).lower(),
                        "residence_lat": self._parse_float(row.get("residence_lat")),
                        "residence_long": self._parse_float(row.get("residence_long")),
                        "work_lat": self._parse_float(row.get("work_lat")),
                        "work_long": self._parse_float(row.get("work_long")),
                    }
                    action = "would import"
                    if not dry_run:
                        patient, created = Patient.objects.update_or_create(id=patient_id, defaults=defaults)
                        ensure_medical_history(patient=patient)
                        action = "created" if created else "updated"
                    count += 1
                    if verbose_created:
                        self.stdout.write(f"{action.title()} patient {patient_id}: {defaults['name']}")
                    if self._should_report_progress(count, progress_every=progress_every):
                        self.stdout.write(f"Processed {count} patients...")
                    self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"Patient row {row.get('id')} failed: {exc}"))
        verb = "Would import" if dry_run else "Imported"
        suffix = " with medical histories" if not dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{verb} {count} patients{suffix}."))

    def _normalize_marital_status(self, value):
        marital_status = self._clean_text(value).lower()
        if marital_status == "divorce":
            return "divorced"
        if marital_status == "widow(er)":
            return "widow"
        return marital_status

    def _validate_visit_dependencies(self, file_path, *, stop_after):
        patient_ids = set()
        doctor_ids = set()
        disease_ids = set()
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            for row_number, row in enumerate(csv.DictReader(csv_file), start=1):
                if stop_after is not None and row_number > stop_after:
                    break
                patient_ids.add(int(row["patient_id"]))
                doctor_ids.add(int(row["doctor_id"]))
                disease_ids.add(int(row["disease_id"]))

        missing_patient_ids = sorted(patient_ids.difference(Patient.objects.filter(id__in=patient_ids).values_list("id", flat=True)))
        missing_doctor_ids = sorted(doctor_ids.difference(Doctor.objects.filter(id__in=doctor_ids).values_list("id", flat=True)))
        missing_disease_ids = sorted(disease_ids.difference(Disease.objects.filter(id__in=disease_ids).values_list("id", flat=True)))

        errors = []
        if missing_patient_ids:
            errors.append(f"missing patients: {missing_patient_ids[:20]}")
        if missing_doctor_ids:
            errors.append(f"missing doctors: {missing_doctor_ids[:20]}")
        if missing_disease_ids:
            errors.append(
                "missing diseases: "
                f"{missing_disease_ids[:20]}. Import/copy diseases into this database before visits."
            )
        if errors:
            raise CommandError("Cannot import visits because dependencies are missing: " + "; ".join(errors))

    def import_visits(
        self,
        file_path,
        *,
        replay_existing_visits,
        delay_seconds,
        progress_every,
        verbose_created,
        stop_after,
        dry_run,
    ):
        self.stdout.write(self.style.NOTICE(f"\n--- Importing Visits & GeoData from {file_path} ---"))
        self._validate_visit_dependencies(file_path, stop_after=stop_after)
        with open(file_path, encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            count = 0
            replayed = 0
            skipped = 0
            for row_number, row in enumerate(reader, start=1):
                if stop_after is not None and row_number > stop_after:
                    break
                try:
                    visit_id = int(row["id"])
                    patient = Patient.objects.get(id=int(row["patient_id"]))
                    visit_data = {
                        "id": visit_id,
                        "doctor_id": int(row["doctor_id"]),
                        "disease_id": int(row["disease_id"]),
                        "diagnosis_date": self._parse_date(row["diagnosis_date"]),
                        "status": self._clean_text(row["status"]).lower(),
                        "weight": self._parse_float(row.get("weight")),
                        "height": self._parse_float(row.get("height")),
                        "marital_status": self._normalize_marital_status(row.get("marital_status")),
                    }

                    existing_visit = Visit.objects.filter(id=visit_id).first()
                    if existing_visit is not None:
                        if not replay_existing_visits:
                            skipped += 1
                            if verbose_created:
                                self.stdout.write(f"Skipped existing visit {visit_id}.")
                            continue

                        if dry_run:
                            replayed += 1
                            if verbose_created or self._should_report_progress(
                                replayed,
                                progress_every=progress_every,
                            ):
                                self.stdout.write(f"Would replay monitoring for existing visit {visit_id}.")
                            continue

                        snapshot = self._monitoring_snapshot()
                        for geodata in GeoData.objects.filter(visit=existing_visit).order_by("id"):
                            run_monitoring_for_geodata(geodata=geodata)
                        delta = self._monitoring_delta(snapshot)
                        geodata_summary = self._visit_geodata_summary(existing_visit)
                        replayed += 1
                        if verbose_created or self._should_report_progress(replayed, progress_every=progress_every):
                            self.stdout.write(
                                self._format_visit_live_summary(
                                    visit=existing_visit,
                                    geodata_summary=geodata_summary,
                                    monitoring_delta=delta,
                                    source="Replayed monitoring for",
                                )
                            )
                        self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                        continue

                    if dry_run:
                        count += 1
                        if verbose_created or self._should_report_progress(count, progress_every=progress_every):
                            self.stdout.write(
                                f"Would create visit {visit_id}: patient={patient.id}, "
                                f"doctor={visit_data['doctor_id']}, disease={visit_data['disease_id']}, "
                                f"status={visit_data['status']}, marital_status={visit_data['marital_status']}"
                            )
                        continue

                    snapshot = self._monitoring_snapshot()
                    with transaction.atomic():
                        visit = create_visit_for_patient(patient=patient, visit_data=visit_data)
                    delta = self._monitoring_delta(snapshot)
                    geodata_summary = self._visit_geodata_summary(visit)

                    count += 1
                    if verbose_created:
                        self.stdout.write(
                            self._format_visit_live_summary(
                                visit=visit,
                                geodata_summary=geodata_summary,
                                monitoring_delta=delta,
                                source="Created",
                            )
                        )
                    if self._should_report_progress(count, progress_every=progress_every):
                        self.stdout.write(f"Processed {count} visits and generated GeoData...")
                    self._sleep_if_needed(delay_seconds=delay_seconds, dry_run=dry_run)
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"Visit row {row.get('id')} failed: {exc}"))

        if dry_run:
            message = f"Would import {count} visits."
        else:
            message = f"Imported {count} visits and generated GeoData."
        self.stdout.write(
            self.style.SUCCESS(
                f"{message} Replayed existing: {replayed}. Skipped existing: {skipped}."
            )
        )
