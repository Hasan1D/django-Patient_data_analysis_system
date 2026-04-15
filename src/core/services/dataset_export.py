import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

from core.models import GeoCluster, Report, Visit


VISIT_LEVEL_FILENAME = "visit_level_dataset.csv"
DISEASE_REGION_DAY_FILENAME = "disease_region_day_dataset.csv"

ACTIVE_ALERT_LEVELS = {"medium", "high", "critical"}
ACTIVE_REPORT_STATUSES = {"new", "reviewed"}
ABNORMAL_RESULT_KEYWORDS = (
    "abnormal",
    "positive",
    "high",
    "low",
    "reactive",
    "detected",
    "critical",
    "elevated",
)
NORMAL_RESULT_KEYWORDS = (
    "normal",
    "negative",
    "within range",
    "within normal",
    "not detected",
)

VISIT_LEVEL_FIELDNAMES = [
    "visit_id",
    "diagnosis_date",
    "patient_id",
    "patient_gender",
    "patient_age_years",
    "doctor_id",
    "hospital_id",
    "hospital_city",
    "disease_id",
    "disease_code",
    "disease_name",
    "disease_type",
    "policy_profile",
    "disease_risk_level",
    "disease_infection_score",
    "disease_high_priority",
    "disease_rare",
    "visit_status",
    "marital_status",
    "weight",
    "height",
    "bmi",
    "primary_region_type",
    "primary_latitude",
    "primary_longitude",
    "geodata_count",
    "has_home_geodata",
    "has_work_geodata",
    "available_region_types",
    "total_lab_test_count",
    "abnormal_lab_test_count",
    "latest_report_alert_level",
    "latest_report_status",
    "latest_report_risk_score",
    "active_report_count",
    "has_active_alert",
]

DISEASE_REGION_DAY_FIELDNAMES = [
    "diagnosis_date",
    "disease_id",
    "disease_code",
    "disease_name",
    "disease_type",
    "policy_profile",
    "disease_high_priority",
    "disease_rare",
    "region_type",
    "visit_count",
    "unique_patient_count",
    "doctor_count",
    "hospital_count",
    "avg_latitude",
    "avg_longitude",
    "total_lab_test_count",
    "abnormal_lab_test_count",
    "active_report_count",
    "critical_alert_count",
    "high_alert_count",
    "medium_alert_count",
    "max_report_risk_score",
    "active_hotspot_count",
]


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


def _safe_round(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    return str(round(value, digits))


def _calculate_age_years(*, birth_date: date, diagnosis_date: date) -> int:
    years = diagnosis_date.year - birth_date.year
    if (diagnosis_date.month, diagnosis_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _calculate_bmi(*, weight: float, height_cm: float) -> float | None:
    if not height_cm:
        return None
    height_m = height_cm / 100
    if height_m <= 0:
        return None
    return weight / (height_m ** 2)


def _is_abnormal_lab_result(result: str) -> bool:
    normalized_result = result.strip().lower()
    if not normalized_result:
        return False
    if "abnormal" in normalized_result:
        return True
    if any(keyword in normalized_result for keyword in NORMAL_RESULT_KEYWORDS):
        return False
    return any(keyword in normalized_result for keyword in ABNORMAL_RESULT_KEYWORDS)


def _preferred_geodata(geodata_records):
    if not geodata_records:
        return None
    home_record = next((record for record in geodata_records if record.region_type == "home"), None)
    return home_record or geodata_records[0]


def _is_active_report(report) -> bool:
    return report.status in ACTIVE_REPORT_STATUSES and report.alert_level in ACTIVE_ALERT_LEVELS


def _write_csv(*, output_path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_visit_level_rows() -> list[dict[str, object]]:
    visits = (
        Visit.objects.select_related("patient", "doctor__hospital", "disease")
        .prefetch_related("geodata_set", "labtest_set", "reports")
        .order_by("id")
    )
    rows: list[dict[str, object]] = []

    for visit in visits:
        geodata_records = list(visit.geodata_set.all().order_by("id"))
        preferred_geodata = _preferred_geodata(geodata_records)
        lab_tests = list(visit.labtest_set.all())
        reports = sorted(list(visit.reports.all()), key=lambda report: report.generated_at, reverse=True)
        latest_report = reports[0] if reports else None
        active_report_count = sum(1 for report in reports if _is_active_report(report))

        available_region_types = sorted({record.region_type for record in geodata_records})
        bmi = _calculate_bmi(weight=visit.weight, height_cm=visit.height)

        rows.append(
            {
                "visit_id": visit.id,
                "diagnosis_date": visit.diagnosis_date.isoformat(),
                "patient_id": visit.patient_id,
                "patient_gender": visit.patient.gender,
                "patient_age_years": _calculate_age_years(
                    birth_date=visit.patient.birth_date,
                    diagnosis_date=visit.diagnosis_date,
                ),
                "doctor_id": visit.doctor_id,
                "hospital_id": visit.doctor.hospital_id,
                "hospital_city": visit.doctor.hospital.city,
                "disease_id": visit.disease_id,
                "disease_code": visit.disease.disease_code,
                "disease_name": visit.disease.name,
                "disease_type": visit.disease.type,
                "policy_profile": visit.disease.policy_profile,
                "disease_risk_level": visit.disease.risk_level,
                "disease_infection_score": visit.disease.infection_score,
                "disease_high_priority": _bool_to_int(visit.disease.high_priority),
                "disease_rare": _bool_to_int(visit.disease.rare_disease),
                "visit_status": visit.status,
                "marital_status": visit.marital_status,
                "weight": visit.weight,
                "height": visit.height,
                "bmi": _safe_round(bmi, 2),
                "primary_region_type": preferred_geodata.region_type if preferred_geodata else "",
                "primary_latitude": _safe_round(getattr(preferred_geodata, "latitude", None)),
                "primary_longitude": _safe_round(getattr(preferred_geodata, "longitude", None)),
                "geodata_count": len(geodata_records),
                "has_home_geodata": _bool_to_int("home" in available_region_types),
                "has_work_geodata": _bool_to_int("work" in available_region_types),
                "available_region_types": ",".join(available_region_types),
                "total_lab_test_count": len(lab_tests),
                "abnormal_lab_test_count": sum(
                    1 for lab_test in lab_tests if _is_abnormal_lab_result(lab_test.result)
                ),
                "latest_report_alert_level": latest_report.alert_level if latest_report else "",
                "latest_report_status": latest_report.status if latest_report else "",
                "latest_report_risk_score": latest_report.risk_score if latest_report else "",
                "active_report_count": active_report_count,
                "has_active_alert": _bool_to_int(active_report_count > 0),
            }
        )

    return rows


def build_disease_region_day_rows() -> list[dict[str, object]]:
    visits = (
        Visit.objects.select_related("patient", "doctor__hospital", "disease")
        .prefetch_related("geodata_set", "labtest_set")
        .order_by("diagnosis_date", "id")
    )
    reports_by_disease: dict[int, list] = defaultdict(list)
    for report in Report.objects.select_related("disease").all():
        reports_by_disease[report.disease_id].append(report)

    active_hotspot_count_by_disease: dict[int, int] = defaultdict(int)
    for cluster in GeoCluster.objects.filter(risk_level__gte=2):
        active_hotspot_count_by_disease[cluster.disease_id] += 1

    grouped_rows: dict[tuple[str, int, str], dict[str, object]] = {}

    for visit in visits:
        geodata_records = list(visit.geodata_set.all().order_by("id"))
        if not geodata_records:
            key = (visit.diagnosis_date.isoformat(), visit.disease_id, "unknown")
            region_records = [None]
        else:
            region_records = geodata_records

        for geodata in region_records:
            region_type = geodata.region_type if geodata else "unknown"
            key = (visit.diagnosis_date.isoformat(), visit.disease_id, region_type)
            group = grouped_rows.setdefault(
                key,
                {
                    "diagnosis_date": visit.diagnosis_date.isoformat(),
                    "disease_id": visit.disease_id,
                    "disease_code": visit.disease.disease_code,
                    "disease_name": visit.disease.name,
                    "disease_type": visit.disease.type,
                    "policy_profile": visit.disease.policy_profile,
                    "disease_high_priority": _bool_to_int(visit.disease.high_priority),
                    "disease_rare": _bool_to_int(visit.disease.rare_disease),
                    "region_type": region_type,
                    "_visit_ids": set(),
                    "_patient_ids": set(),
                    "_doctor_ids": set(),
                    "_hospital_ids": set(),
                    "_latitudes": [],
                    "_longitudes": [],
                    "_total_lab_test_count": 0,
                    "_abnormal_lab_test_count": 0,
                    "_active_report_count": 0,
                    "_critical_alert_count": 0,
                    "_high_alert_count": 0,
                    "_medium_alert_count": 0,
                    "_max_report_risk_score": 0.0,
                },
            )

            if visit.id in group["_visit_ids"]:
                if geodata:
                    group["_latitudes"].append(geodata.latitude)
                    group["_longitudes"].append(geodata.longitude)
                continue

            group["_visit_ids"].add(visit.id)
            group["_patient_ids"].add(visit.patient_id)
            group["_doctor_ids"].add(visit.doctor_id)
            group["_hospital_ids"].add(visit.doctor.hospital_id)
            if geodata:
                group["_latitudes"].append(geodata.latitude)
                group["_longitudes"].append(geodata.longitude)

            lab_tests = list(visit.labtest_set.all())
            group["_total_lab_test_count"] += len(lab_tests)
            group["_abnormal_lab_test_count"] += sum(
                1 for lab_test in lab_tests if _is_abnormal_lab_result(lab_test.result)
            )

            matching_reports = [
                report
                for report in reports_by_disease.get(visit.disease_id, [])
                if report.analysis_period_start <= visit.diagnosis_date <= report.analysis_period_end
                and _is_active_report(report)
            ]
            group["_active_report_count"] += len(matching_reports)
            group["_critical_alert_count"] += sum(1 for report in matching_reports if report.alert_level == "critical")
            group["_high_alert_count"] += sum(1 for report in matching_reports if report.alert_level == "high")
            group["_medium_alert_count"] += sum(1 for report in matching_reports if report.alert_level == "medium")
            if matching_reports:
                group["_max_report_risk_score"] = max(
                    group["_max_report_risk_score"],
                    max(report.risk_score for report in matching_reports),
                )

    output_rows: list[dict[str, object]] = []
    for group in grouped_rows.values():
        latitudes = group.pop("_latitudes")
        longitudes = group.pop("_longitudes")
        visit_ids = group.pop("_visit_ids")
        patient_ids = group.pop("_patient_ids")
        doctor_ids = group.pop("_doctor_ids")
        hospital_ids = group.pop("_hospital_ids")
        total_lab_test_count = group.pop("_total_lab_test_count")
        abnormal_lab_test_count = group.pop("_abnormal_lab_test_count")
        active_report_count = group.pop("_active_report_count")
        critical_alert_count = group.pop("_critical_alert_count")
        high_alert_count = group.pop("_high_alert_count")
        medium_alert_count = group.pop("_medium_alert_count")
        max_report_risk_score = group.pop("_max_report_risk_score")

        group.update(
            {
                "visit_count": len(visit_ids),
                "unique_patient_count": len(patient_ids),
                "doctor_count": len(doctor_ids),
                "hospital_count": len(hospital_ids),
                "avg_latitude": _safe_round(sum(latitudes) / len(latitudes)) if latitudes else "",
                "avg_longitude": _safe_round(sum(longitudes) / len(longitudes)) if longitudes else "",
                "total_lab_test_count": total_lab_test_count,
                "abnormal_lab_test_count": abnormal_lab_test_count,
                "active_report_count": active_report_count,
                "critical_alert_count": critical_alert_count,
                "high_alert_count": high_alert_count,
                "medium_alert_count": medium_alert_count,
                "max_report_risk_score": max_report_risk_score,
                "active_hotspot_count": active_hotspot_count_by_disease[group["disease_id"]],
            }
        )
        output_rows.append(group)

    output_rows.sort(key=lambda row: (row["diagnosis_date"], row["disease_code"], row["region_type"]))
    return output_rows


def export_ai_datasets(*, output_dir: str | Path) -> dict[str, object]:
    output_directory = Path(output_dir)
    output_directory.mkdir(parents=True, exist_ok=True)

    visit_rows = build_visit_level_rows()
    disease_region_day_rows = build_disease_region_day_rows()

    visit_level_path = output_directory / VISIT_LEVEL_FILENAME
    disease_region_day_path = output_directory / DISEASE_REGION_DAY_FILENAME

    _write_csv(output_path=visit_level_path, fieldnames=VISIT_LEVEL_FIELDNAMES, rows=visit_rows)
    _write_csv(
        output_path=disease_region_day_path,
        fieldnames=DISEASE_REGION_DAY_FIELDNAMES,
        rows=disease_region_day_rows,
    )

    return {
        "output_dir": str(output_directory.resolve()),
        "visit_level_path": str(visit_level_path.resolve()),
        "visit_level_count": len(visit_rows),
        "disease_region_day_path": str(disease_region_day_path.resolve()),
        "disease_region_day_count": len(disease_region_day_rows),
    }
