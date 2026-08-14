import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from core.models import Report

from .features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    build_report_feature_map,
    feature_vector,
    normalize_feature_map,
)


LABEL_ORDER = ["low", "medium", "high", "critical"]
MODEL_TYPE = "RandomForestClassifier"


@dataclass(frozen=True)
class RandomForestTrainingSample:
    report_id: int
    label: str
    features: dict[str, float]


@dataclass(frozen=True)
class RandomForestTrainingResult:
    model_path: str
    metadata_path: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class RandomForestPrediction:
    predicted_alert_level: str
    confidence: float
    probabilities: dict[str, float]
    used_features: dict[str, float]


def _artifact_root() -> Path:
    return Path(__file__).resolve().parents[4] / "artifacts"


def resolve_default_model_path() -> Path:
    return _artifact_root() / "random_forest_alert_model.joblib"


def resolve_default_metadata_path(*, model_path: str | Path | None = None) -> Path:
    resolved_model_path = Path(model_path) if model_path is not None else resolve_default_model_path()
    return resolved_model_path.with_suffix(".metadata.json")


def _load_ml_dependencies():
    import joblib
    import numpy as np
    import sklearn
    from sklearn.ensemble import RandomForestClassifier

    return np, joblib, RandomForestClassifier, sklearn.__version__


def build_training_samples() -> list[RandomForestTrainingSample]:
    reports = (
        Report.objects.select_related("disease")
        .filter(alert_level__in=LABEL_ORDER)
        .order_by("id")
    )
    return [
        RandomForestTrainingSample(
            report_id=report.id,
            label=report.alert_level,
            features=build_report_feature_map(report=report),
        )
        for report in reports
    ]


def train_random_forest_model(
    *,
    output_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
    n_estimators: int = 100,
    random_state: int = 42,
) -> RandomForestTrainingResult:
    samples = build_training_samples()
    if len(samples) < 2:
        raise ValueError("At least 2 training samples are required to train the random forest model.")

    labels = [sample.label for sample in samples]
    if len(set(labels)) < 2:
        raise ValueError("At least 2 distinct alert levels are required to train the random forest model.")

    np, joblib, RandomForestClassifier, sklearn_version = _load_ml_dependencies()
    feature_matrix = np.array([feature_vector(sample.features) for sample in samples], dtype=float)

    model = RandomForestClassifier(
        n_estimators=n_estimators,
        random_state=random_state,
        class_weight="balanced",
    )
    model.fit(feature_matrix, labels)

    resolved_model_path = Path(output_path) if output_path is not None else resolve_default_model_path()
    resolved_metadata_path = Path(metadata_path) if metadata_path is not None else resolve_default_metadata_path(
        model_path=resolved_model_path
    )
    resolved_model_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_metadata_path.parent.mkdir(parents=True, exist_ok=True)

    model_payload = {
        "model": model,
        "feature_names": FEATURE_NAMES,
        "label_order": LABEL_ORDER,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }
    joblib.dump(model_payload, resolved_model_path)

    metadata = {
        "feature_names": FEATURE_NAMES,
        "label_order": LABEL_ORDER,
        "trained_at": datetime.utcnow().isoformat() + "Z",
        "sample_count": len(samples),
        "model_type": MODEL_TYPE,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "sklearn_version": sklearn_version,
        "n_estimators": n_estimators,
        "random_state": random_state,
        "label_counts": {label: labels.count(label) for label in LABEL_ORDER if label in labels},
        "sample_report_ids": [sample.report_id for sample in samples],
    }
    resolved_metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return RandomForestTrainingResult(
        model_path=str(resolved_model_path.resolve()),
        metadata_path=str(resolved_metadata_path.resolve()),
        metadata=metadata,
    )


def load_random_forest_model(
    *,
    model_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    _, joblib, _, _ = _load_ml_dependencies()
    resolved_model_path = Path(model_path) if model_path is not None else resolve_default_model_path()
    resolved_metadata_path = Path(metadata_path) if metadata_path is not None else resolve_default_metadata_path(
        model_path=resolved_model_path
    )
    if not resolved_model_path.exists():
        raise FileNotFoundError(f"Random forest model artifact was not found at {resolved_model_path}")
    if not resolved_metadata_path.exists():
        raise FileNotFoundError(f"Random forest metadata artifact was not found at {resolved_metadata_path}")

    payload = joblib.load(resolved_model_path)
    metadata = json.loads(resolved_metadata_path.read_text(encoding="utf-8"))
    return payload, metadata


def predict_alert_level(
    *,
    features: dict[str, object],
    model_path: str | Path | None = None,
    metadata_path: str | Path | None = None,
) -> RandomForestPrediction:
    np, _, _, _ = _load_ml_dependencies()
    payload, metadata = load_random_forest_model(model_path=model_path, metadata_path=metadata_path)

    expected_features = payload.get("feature_names") or metadata.get("feature_names")
    if expected_features != FEATURE_NAMES:
        raise ValueError("Random forest feature schema does not match the current code.")

    model = payload["model"]
    used_features = normalize_feature_map(features)
    feature_matrix = np.array([feature_vector(used_features)], dtype=float)
    predicted_label = str(model.predict(feature_matrix)[0])

    probabilities = {label: 0.0 for label in LABEL_ORDER}
    if hasattr(model, "predict_proba"):
        class_probabilities = model.predict_proba(feature_matrix)[0]
        for model_class, probability in zip(model.classes_, class_probabilities):
            probabilities[str(model_class)] = round(float(probability), 4)

    confidence = probabilities.get(predicted_label, 0.0)
    if confidence == 0.0:
        confidence = 1.0

    return RandomForestPrediction(
        predicted_alert_level=predicted_label,
        confidence=round(float(confidence), 4),
        probabilities=probabilities,
        used_features=used_features,
    )


def serialize_random_forest_prediction(prediction: RandomForestPrediction) -> dict[str, object]:
    return asdict(prediction)
