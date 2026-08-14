from core.models import Disease, Report
from core.services.contracts import TrendSnapshot


FEATURE_SCHEMA_VERSION = "rf_alert_features_v1"

FEATURE_NAMES = [
    "nearby_case_count",
    "current_case_count",
    "previous_case_count",
    "growth_rate",
    "surge_ratio",
    "rule_based_risk_score",
    "disease_risk_level",
    "infection_score",
    "high_priority",
    "rare_disease",
    "hdbscan_cluster_detected",
    "hdbscan_cluster_size",
    "hdbscan_cluster_probability",
    "hdbscan_cluster_radius_km",
]

DEFAULT_HDBSCAN_FEATURES = {
    "hdbscan_cluster_detected": 0.0,
    "hdbscan_cluster_size": 0.0,
    "hdbscan_cluster_probability": 0.0,
    "hdbscan_cluster_radius_km": 0.0,
}


def _bool_to_float(value: bool) -> float:
    return 1.0 if value else 0.0


def normalize_feature_map(features: dict[str, object]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for feature_name in FEATURE_NAMES:
        value = features.get(feature_name, 0.0)
        try:
            normalized[feature_name] = float(value or 0.0)
        except (TypeError, ValueError):
            normalized[feature_name] = 0.0
    return normalized


def feature_vector(features: dict[str, object]) -> list[float]:
    normalized = normalize_feature_map(features)
    return [normalized[feature_name] for feature_name in FEATURE_NAMES]


def build_outbreak_feature_map(
    *,
    disease: Disease,
    nearby_case_count: int,
    local_case_count: int,
    trend: TrendSnapshot,
    rule_based_risk_score: float,
    hdbscan_features: dict[str, object] | None = None,
) -> dict[str, float]:
    features = {
        "nearby_case_count": nearby_case_count,
        "current_case_count": local_case_count,
        "previous_case_count": trend.previous_count,
        "growth_rate": trend.growth_rate,
        "surge_ratio": trend.surge_ratio,
        "rule_based_risk_score": rule_based_risk_score,
        "disease_risk_level": disease.risk_level,
        "infection_score": disease.infection_score,
        "high_priority": _bool_to_float(disease.high_priority),
        "rare_disease": _bool_to_float(disease.rare_disease),
        **DEFAULT_HDBSCAN_FEATURES,
    }
    if hdbscan_features:
        features.update(hdbscan_features)
    return normalize_feature_map(features)


def build_report_feature_map(*, report: Report) -> dict[str, float]:
    disease = report.disease
    features = {
        "nearby_case_count": report.nearby_case_count,
        "current_case_count": report.current_case_count,
        "previous_case_count": report.previous_case_count,
        "growth_rate": report.growth_rate,
        "surge_ratio": report.surge_ratio,
        "rule_based_risk_score": report.risk_score,
        "disease_risk_level": disease.risk_level,
        "infection_score": disease.infection_score,
        "high_priority": _bool_to_float(disease.high_priority),
        "rare_disease": _bool_to_float(disease.rare_disease),
        **DEFAULT_HDBSCAN_FEATURES,
    }
    return normalize_feature_map(features)
