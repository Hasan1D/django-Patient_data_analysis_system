from dataclasses import dataclass, field
from datetime import date
from typing import Literal


AlertLevel = Literal["no_alert", "low", "medium", "high", "critical"]


@dataclass(frozen=True)
class VisitOutbreakContext:
    visit_id: int
    disease_id: int
    disease_code: str
    diagnosis_date: date
    latitude: float
    longitude: float
    region_type: str
    patient_id: int | None = None
    doctor_id: int | None = None


@dataclass(frozen=True)
class NearbyCase:
    visit_id: int
    patient_id: int
    diagnosis_date: date
    latitude: float
    longitude: float
    region_type: str
    distance_km: float


@dataclass(frozen=True)
class TrendSnapshot:
    current_count: int
    previous_count: int
    growth_rate: float
    surge_ratio: float


@dataclass(frozen=True)
class DiseaseAlertPolicy:
    disease_code: str
    lookback_days: int = 7
    baseline_window_days: int = 7
    radius_km: float = 3.0
    region_type: str | None = None
    rare_disease: bool = False
    high_priority: bool = False
    severity_weight: float = 1.0
    density_weight: float = 1.0
    trend_weight: float = 1.0
    rarity_weight: float = 1.0
    cluster_case_threshold: int = 3
    critical_case_threshold: int = 6


@dataclass(frozen=True)
class OutbreakAnalysis:
    alert_level: AlertLevel
    score: float
    reasons: list[str] = field(default_factory=list)
    nearby_case_count: int = 0
    matched_case_ids: list[int] = field(default_factory=list)
    trend: TrendSnapshot | None = None
    should_create_report: bool = False
    should_create_cluster: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
