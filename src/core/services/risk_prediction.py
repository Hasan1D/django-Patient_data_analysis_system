import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from core.models import Visit

from .active_cases import active_visits_queryset, is_active_visit_in_queryset
from .alert_policies import get_policy_for_disease
from .outbreak_engine import evaluate_visit_outbreak
from .spatial import distance_km
from .trend import build_trend_snapshot


LABEL_ORDER = ["low", "medium", "high", "critical"]
POLICY_PROFILES = [
    "general",
    "high_priority",
    "rare",
    "cluster_sensitive",
    "surge_sensitive",
    "environmental_signal",
]


@dataclass(frozen=True)
class RiskTrainingSample:
    visit_id: int
    label: str
    features: dict[str, float]
    label_source: str


@dataclass(frozen=True)
class RiskPrediction:
    visit_id: int
    predicted_label: str
    confidence: float
    class_distances: dict[str, float]
    feature_values: dict[str, float]
    label_source: str | None = None


def resolve_default_model_path() -> Path:
    return Path(__file__).resolve().parents[3] / "artifacts" / "risk_model.json"


def _calculate_age_years(*, birth_date, diagnosis_date) -> int:
    years = diagnosis_date.year - birth_date.year
    if (diagnosis_date.month, diagnosis_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _calculate_bmi(*, weight: float, height_cm: float) -> float:
    if not height_cm:
        return 0.0
    height_m = height_cm / 100
    if height_m <= 0:
        return 0.0
    return round(weight / (height_m ** 2), 4)


def _is_active_report(report) -> bool:
    return report.status in {"new", "reviewed"} and report.alert_level in {"medium", "high", "critical"}


def _is_abnormal_lab_result(result: str) -> bool:
    normalized_result = result.strip().lower()
    if not normalized_result:
        return False
    if "abnormal" in normalized_result:
        return True
    if any(token in normalized_result for token in ("normal", "negative", "within range", "not detected")):
        return False
    return any(token in normalized_result for token in ("high", "low", "positive", "critical", "detected", "elevated"))


def _preferred_geodata(geodata_records):
    if not geodata_records:
        return None
    home_record = next((record for record in geodata_records if record.region_type == "home"), None)
    return home_record or geodata_records[0]


def _policy_profile_features(policy_profile: str) -> dict[str, float]:
    return {
        f"profile_{profile}": 1.0 if policy_profile == profile else 0.0
        for profile in POLICY_PROFILES
    }


def _recent_cluster_features(*, visit) -> tuple[float, float]:
    matching_clusters = visit.disease.geocluster_set.order_by("-generated_at")[:5]
    geodata_records = list(visit.geodata_set.all().order_by("id"))
    preferred_geodata = _preferred_geodata(geodata_records)
    if preferred_geodata is None or not matching_clusters:
        return 0.0, 0.0

    min_distance = min(
        distance_km(
            preferred_geodata.latitude,
            preferred_geodata.longitude,
            cluster.center_lat,
            cluster.center_long,
        )
        for cluster in matching_clusters
    )
    hotspot_nearby = any(
        distance_km(
            preferred_geodata.latitude,
            preferred_geodata.longitude,
            cluster.center_lat,
            cluster.center_long,
        ) <= max(cluster.radius, 3.0)
        for cluster in matching_clusters
    )
    return round(min_distance, 4), 1.0 if hotspot_nearby else 0.0


def build_visit_feature_map(*, visit: Visit) -> dict[str, float]:
    geodata_records = list(visit.geodata_set.all().order_by("id"))
    preferred_geodata = _preferred_geodata(geodata_records)
    lab_tests = list(visit.labtest_set.all())
    reports = list(visit.reports.all().order_by("-generated_at"))
    active_reports = [report for report in reports if _is_active_report(report)]
    latest_report = reports[0] if reports else None
    age_years = _calculate_age_years(
        birth_date=visit.patient.birth_date,
        diagnosis_date=visit.diagnosis_date,
    )
    bmi = _calculate_bmi(weight=visit.weight, height_cm=visit.height)
    recent_cluster_distance_km, hotspot_nearby = _recent_cluster_features(visit=visit)

    features = {
        "disease_risk_level": float(visit.disease.risk_level),
        "disease_infection_score": float(visit.disease.infection_score),
        "disease_high_priority": 1.0 if visit.disease.high_priority else 0.0,
        "disease_rare": 1.0 if visit.disease.rare_disease else 0.0,
        "patient_age_years": float(age_years),
        "bmi": float(bmi),
        "geodata_count": float(len(geodata_records)),
        "has_home_geodata": 1.0 if any(record.region_type == "home" for record in geodata_records) else 0.0,
        "has_work_geodata": 1.0 if any(record.region_type == "work" for record in geodata_records) else 0.0,
        "total_lab_test_count": float(len(lab_tests)),
        "abnormal_lab_test_count": float(
            sum(1 for lab_test in lab_tests if _is_abnormal_lab_result(lab_test.result))
        ),
        "latest_report_risk_score": float(latest_report.risk_score if latest_report else 0.0),
        "active_report_count": float(len(active_reports)),
        "has_active_alert": 1.0 if active_reports else 0.0,
        "primary_region_home": 1.0 if preferred_geodata and preferred_geodata.region_type == "home" else 0.0,
        "primary_region_work": 1.0 if preferred_geodata and preferred_geodata.region_type == "work" else 0.0,
        "recent_cluster_distance_km": float(recent_cluster_distance_km),
        "hotspot_nearby": float(hotspot_nearby),
    }
    features.update(_policy_profile_features(visit.disease.policy_profile))
    return features


def _label_from_reports(*, visit: Visit) -> tuple[str | None, str | None]:
    reports = list(visit.reports.all())
    if not reports:
        return None, None

    highest_report = max(
        reports,
        key=lambda report: LABEL_ORDER.index(report.alert_level) if report.alert_level in LABEL_ORDER else -1,
    )
    if highest_report.alert_level in LABEL_ORDER:
        return highest_report.alert_level, "report"
    return None, None


def _label_from_engine(*, visit: Visit) -> tuple[str, str]:
    geodata = _preferred_geodata(list(visit.geodata_set.all().order_by("id")))
    if geodata is None:
        return "low", "default"

    policy = get_policy_for_disease(
        disease_code=visit.disease.disease_code,
        risk_level=visit.disease.risk_level,
        infection_score=visit.disease.infection_score,
        high_priority=visit.disease.high_priority,
        rare_disease=visit.disease.rare_disease,
        policy_profile=visit.disease.policy_profile,
    )
    context = {
        "visit_id": visit.id,
        "patient_id": visit.patient_id,
        "doctor_id": visit.doctor_id,
        "disease_id": visit.disease_id,
        "disease_code": visit.disease.disease_code,
        "diagnosis_date": visit.diagnosis_date,
        "latitude": geodata.latitude,
        "longitude": geodata.longitude,
        "region_type": geodata.region_type,
    }
    analysis = evaluate_visit_outbreak(context=type("Context", (), context)(), policy=policy)
    return analysis.alert_level if analysis.alert_level in LABEL_ORDER else "low", "engine"


def build_training_samples() -> list[RiskTrainingSample]:
    visits = active_visits_queryset(
        Visit.objects.select_related("patient", "doctor__hospital", "disease")
        .prefetch_related("geodata_set", "labtest_set", "reports", "disease__geocluster_set")
        .order_by("id")
    )

    samples: list[RiskTrainingSample] = []
    for visit in visits:
        label, label_source = _label_from_reports(visit=visit)
        if label is None:
            label, label_source = _label_from_engine(visit=visit)

        if label not in LABEL_ORDER:
            continue

        samples.append(
            RiskTrainingSample(
                visit_id=visit.id,
                label=label,
                features=build_visit_feature_map(visit=visit),
                label_source=label_source or "default",
            )
        )

    return samples


def _normalize_features(*, features: dict[str, float], feature_min: dict[str, float], feature_max: dict[str, float]) -> dict[str, float]:
    normalized = {}
    for feature_name, value in features.items():
        min_value = feature_min[feature_name]
        max_value = feature_max[feature_name]
        if max_value == min_value:
            normalized[feature_name] = 0.0
        else:
            normalized[feature_name] = round((value - min_value) / (max_value - min_value), 6)
    return normalized


def _euclidean_distance(features_a: dict[str, float], features_b: dict[str, float]) -> float:
    return math.sqrt(
        sum((features_a[key] - features_b[key]) ** 2 for key in features_a)
    )


def train_baseline_risk_model(*, output_path: str | Path | None = None) -> dict[str, object]:
    samples = build_training_samples()
    if len(samples) < 2:
        raise ValueError("At least 2 training samples are required to train the risk model.")

    label_counts = Counter(sample.label for sample in samples)
    if len(label_counts) < 2:
        raise ValueError("At least 2 distinct labels are required to train the risk model.")

    feature_names = sorted(samples[0].features.keys())
    feature_min = {
        feature_name: min(sample.features[feature_name] for sample in samples)
        for feature_name in feature_names
    }
    feature_max = {
        feature_name: max(sample.features[feature_name] for sample in samples)
        for feature_name in feature_names
    }

    normalized_samples = [
        {
            "visit_id": sample.visit_id,
            "label": sample.label,
            "label_source": sample.label_source,
            "features": _normalize_features(
                features=sample.features,
                feature_min=feature_min,
                feature_max=feature_max,
            ),
        }
        for sample in samples
    ]

    class_centroids: dict[str, dict[str, float]] = {}
    for label in LABEL_ORDER:
        labeled_samples = [sample for sample in normalized_samples if sample["label"] == label]
        if not labeled_samples:
            continue
        class_centroids[label] = {
            feature_name: round(
                sum(sample["features"][feature_name] for sample in labeled_samples) / len(labeled_samples),
                6,
            )
            for feature_name in feature_names
        }

    artifact = {
        "algorithm": "centroid_classifier",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "feature_names": feature_names,
        "feature_min": feature_min,
        "feature_max": feature_max,
        "class_centroids": class_centroids,
        "label_counts": dict(label_counts),
        "sample_count": len(samples),
        "sample_visit_ids": [sample.visit_id for sample in samples],
        "label_sources": dict(Counter(sample.label_source for sample in samples)),
    }

    model_path = Path(output_path) if output_path is not None else resolve_default_model_path()
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return {
        "artifact": artifact,
        "model_path": str(model_path.resolve()),
    }


def load_risk_model(*, model_path: str | Path | None = None) -> dict[str, object]:
    resolved_path = Path(model_path) if model_path is not None else resolve_default_model_path()
    if not resolved_path.exists():
        raise FileNotFoundError(f"Risk model artifact was not found at {resolved_path}")
    return json.loads(resolved_path.read_text(encoding="utf-8"))


def predict_visit_risk(*, visit: Visit, model_path: str | Path | None = None) -> RiskPrediction:
    feature_values = build_visit_feature_map(visit=visit)
    if not is_active_visit_in_queryset(visit_id=visit.id):
        return RiskPrediction(
            visit_id=visit.id,
            predicted_label="low",
            confidence=1.0,
            class_distances={},
            feature_values=feature_values,
            label_source="inactive_status",
        )

    artifact = load_risk_model(model_path=model_path)
    normalized_features = _normalize_features(
        features=feature_values,
        feature_min=artifact["feature_min"],
        feature_max=artifact["feature_max"],
    )

    class_distances = {
        label: round(_euclidean_distance(normalized_features, centroid), 6)
        for label, centroid in artifact["class_centroids"].items()
    }
    if not class_distances:
        raise ValueError("Risk model does not contain any trained class centroids.")

    predicted_label = min(class_distances, key=class_distances.get)
    inverse_distances = {
        label: 1 / (distance + 1e-6)
        for label, distance in class_distances.items()
    }
    inverse_distance_total = sum(inverse_distances.values())
    confidence = round(inverse_distances[predicted_label] / inverse_distance_total, 4)

    label, label_source = _label_from_reports(visit=visit)
    if label is None:
        label_source = None

    return RiskPrediction(
        visit_id=visit.id,
        predicted_label=predicted_label,
        confidence=confidence,
        class_distances=class_distances,
        feature_values=feature_values,
        label_source=label_source,
    )


def serialize_risk_prediction(prediction: RiskPrediction) -> dict[str, object]:
    return asdict(prediction)
