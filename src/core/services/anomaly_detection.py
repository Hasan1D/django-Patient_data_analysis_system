from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta
from statistics import median

from django.db.models import Count, Max

from core.models import GeoData


@dataclass(frozen=True)
class AnomalyCandidate:
    disease_id: int
    disease_code: str
    disease_name: str
    policy_profile: str
    region_type: str
    target_date: str
    baseline_start: str
    baseline_end: str
    observed_count: int
    baseline_mean: float
    baseline_median: float
    baseline_max: int
    surge_ratio: float
    deviation_score: float
    anomaly_score: float
    severity: str
    reasons: list[str]


def _resolve_series_window(*, disease_id: int | None, lookback_days: int | None, date_from, date_to):
    if date_from or date_to or lookback_days is None:
        return date_from, date_to

    queryset = GeoData.objects.all()
    if disease_id is not None:
        queryset = queryset.filter(visit__disease_id=disease_id)

    latest_date = queryset.aggregate(max_date=Max("visit__diagnosis_date"))["max_date"]
    if latest_date is None:
        return None, None

    return latest_date - timedelta(days=lookback_days), latest_date


def _series_rows(*, disease_id: int | None, region_type: str | None, date_from=None, date_to=None):
    resolved_date_from, resolved_date_to = _resolve_series_window(
        disease_id=disease_id,
        lookback_days=None,
        date_from=date_from,
        date_to=date_to,
    )

    queryset = GeoData.objects.select_related("visit__disease").all()
    if disease_id is not None:
        queryset = queryset.filter(visit__disease_id=disease_id)
    if resolved_date_from is not None:
        queryset = queryset.filter(visit__diagnosis_date__gte=resolved_date_from)
    if resolved_date_to is not None:
        queryset = queryset.filter(visit__diagnosis_date__lte=resolved_date_to)
    if region_type:
        queryset = queryset.filter(region_type=region_type)

    return list(
        queryset.values(
            "visit__disease_id",
            "visit__disease__disease_code",
            "visit__disease__name",
            "visit__disease__policy_profile",
            "visit__disease__high_priority",
            "visit__disease__rare_disease",
            "region_type",
            "visit__diagnosis_date",
        )
        .annotate(total_cases=Count("visit", distinct=True))
        .order_by("visit__disease_id", "region_type", "visit__diagnosis_date")
    )


def _daterange(start_date, end_date):
    current_date = start_date
    while current_date <= end_date:
        yield current_date
        current_date += timedelta(days=1)


def _mean(values: list[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _deviation_score(*, observed_count: int, baseline_median: float, baseline_values: list[int]) -> float:
    deviations = [abs(value - baseline_median) for value in baseline_values]
    median_absolute_deviation = median(deviations) if deviations else 0.0
    if median_absolute_deviation == 0:
        return max(float(observed_count - baseline_median), 0.0)
    return max(0.6745 * (observed_count - baseline_median) / median_absolute_deviation, 0.0)


def _minimum_observed_threshold(*, policy_profile: str, high_priority: bool, rare_disease: bool) -> int:
    if rare_disease:
        return 1
    if high_priority:
        return 2
    if policy_profile == "cluster_sensitive":
        return 2
    if policy_profile == "surge_sensitive":
        return 2
    return 3


def _build_reasons(
    *,
    observed_count: int,
    baseline_mean: float,
    surge_ratio: float,
    deviation_score: float,
    high_priority: bool,
    rare_disease: bool,
) -> list[str]:
    reasons = [
        f"Observed {observed_count} case(s) against a baseline mean of {round(baseline_mean, 2)}.",
    ]
    if surge_ratio >= 2.0:
        reasons.append(f"Surge ratio reached {round(surge_ratio, 2)}.")
    if deviation_score >= 3.0:
        reasons.append(f"Deviation score reached {round(deviation_score, 2)}.")
    if rare_disease:
        reasons.append("Rare-disease profile increases anomaly sensitivity.")
    elif high_priority:
        reasons.append("High-priority disease profile increases anomaly sensitivity.")
    return reasons


def _determine_severity(*, anomaly_score: float, rare_disease: bool) -> str:
    if rare_disease and anomaly_score >= 25:
        return "critical"
    if anomaly_score >= 80:
        return "critical"
    if anomaly_score >= 55:
        return "high"
    if anomaly_score >= 35:
        return "medium"
    return "low"


def detect_temporal_anomalies(
    *,
    disease_id: int | None = None,
    lookback_days: int = 30,
    baseline_window_days: int = 7,
    region_type: str | None = None,
) -> list[AnomalyCandidate]:
    if lookback_days < 2:
        raise ValueError("lookback_days must be at least 2.")
    if baseline_window_days < 2:
        raise ValueError("baseline_window_days must be at least 2.")

    date_from, date_to = _resolve_series_window(
        disease_id=disease_id,
        lookback_days=lookback_days,
        date_from=None,
        date_to=None,
    )
    if date_from is None or date_to is None:
        return []

    rows = _series_rows(
        disease_id=disease_id,
        region_type=region_type,
        date_from=date_from,
        date_to=date_to,
    )
    if not rows:
        return []

    grouped_series: dict[tuple[int, str], dict[str, object]] = {}
    for row in rows:
        key = (row["visit__disease_id"], row["region_type"])
        grouped_series.setdefault(
            key,
            {
                "disease_id": row["visit__disease_id"],
                "disease_code": row["visit__disease__disease_code"],
                "disease_name": row["visit__disease__name"],
                "policy_profile": row["visit__disease__policy_profile"],
                "high_priority": row["visit__disease__high_priority"],
                "rare_disease": row["visit__disease__rare_disease"],
                "region_type": row["region_type"],
                "counts_by_date": {},
            },
        )
        grouped_series[key]["counts_by_date"][row["visit__diagnosis_date"]] = row["total_cases"]

    candidates: list[AnomalyCandidate] = []
    for series in grouped_series.values():
        counts_by_date = series["counts_by_date"]
        earliest_date = min(counts_by_date)
        latest_date = max(counts_by_date)
        daily_counts = {series_date: counts_by_date.get(series_date, 0) for series_date in _daterange(earliest_date, latest_date)}
        ordered_dates = sorted(daily_counts)

        if len(ordered_dates) <= baseline_window_days:
            continue

        minimum_observed = _minimum_observed_threshold(
            policy_profile=series["policy_profile"],
            high_priority=series["high_priority"],
            rare_disease=series["rare_disease"],
        )

        for index in range(baseline_window_days, len(ordered_dates)):
            current_date = ordered_dates[index]
            observed_count = daily_counts[current_date]
            if observed_count <= 0:
                continue

            baseline_dates = ordered_dates[index - baseline_window_days:index]
            baseline_values = [daily_counts[baseline_date] for baseline_date in baseline_dates]
            baseline_mean = _mean(baseline_values)
            baseline_median = median(baseline_values)
            baseline_max = max(baseline_values)
            surge_ratio = round(
                observed_count / baseline_mean,
                2,
            ) if baseline_mean > 0 else float(observed_count)
            deviation_score = round(
                _deviation_score(
                    observed_count=observed_count,
                    baseline_median=baseline_median,
                    baseline_values=baseline_values,
                ),
                2,
            )

            anomaly_score = 0.0
            if baseline_mean == 0 and observed_count >= minimum_observed:
                anomaly_score += min(observed_count * 10, 35)
            else:
                anomaly_score += min(max(observed_count - baseline_mean, 0) * 12, 35)

            if surge_ratio >= 2.0:
                anomaly_score += min((surge_ratio - 1.0) * 18, 25)
            if deviation_score >= 2.0:
                anomaly_score += min(deviation_score * 6, 20)
            if series["high_priority"]:
                anomaly_score += 10
            if series["rare_disease"]:
                anomaly_score += 20

            anomaly_score = round(min(anomaly_score, 100.0), 2)
            severity = _determine_severity(
                anomaly_score=anomaly_score,
                rare_disease=series["rare_disease"],
            )

            triggered = False
            if series["rare_disease"] and observed_count >= 1:
                triggered = True
            elif observed_count >= minimum_observed and (
                baseline_mean == 0
                or surge_ratio >= 2.0
                or deviation_score >= 3.0
                or observed_count >= baseline_max + 2
            ):
                triggered = True

            if not triggered or severity == "low":
                continue

            candidates.append(
                AnomalyCandidate(
                    disease_id=series["disease_id"],
                    disease_code=series["disease_code"],
                    disease_name=series["disease_name"],
                    policy_profile=series["policy_profile"],
                    region_type=series["region_type"],
                    target_date=current_date.isoformat(),
                    baseline_start=baseline_dates[0].isoformat(),
                    baseline_end=baseline_dates[-1].isoformat(),
                    observed_count=observed_count,
                    baseline_mean=round(baseline_mean, 2),
                    baseline_median=round(float(baseline_median), 2),
                    baseline_max=baseline_max,
                    surge_ratio=surge_ratio,
                    deviation_score=deviation_score,
                    anomaly_score=anomaly_score,
                    severity=severity,
                    reasons=_build_reasons(
                        observed_count=observed_count,
                        baseline_mean=baseline_mean,
                        surge_ratio=surge_ratio,
                        deviation_score=deviation_score,
                        high_priority=series["high_priority"],
                        rare_disease=series["rare_disease"],
                    ),
                )
            )

    candidates.sort(key=lambda candidate: (-candidate.anomaly_score, candidate.target_date, candidate.disease_code))
    return candidates


def serialize_anomaly_candidates(candidates: list[AnomalyCandidate]) -> list[dict[str, object]]:
    return [asdict(candidate) for candidate in candidates]
