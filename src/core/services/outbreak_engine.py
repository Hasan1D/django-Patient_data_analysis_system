from datetime import timedelta

from core.models import Visit

from .active_cases import is_active_visit_in_queryset
from .contracts import AlertLevel, DiseaseAlertPolicy, OutbreakAnalysis, TrendSnapshot, VisitOutbreakContext
from .spatial import find_nearby_cases
from .trend import build_trend_snapshot


def _build_time_windows(
    *,
    diagnosis_date,
    lookback_days: int,
    baseline_window_days: int,
) -> tuple:
    current_end = diagnosis_date
    current_start = current_end - timedelta(days=lookback_days)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=baseline_window_days)
    return current_start, current_end, previous_start, previous_end


def _density_factor(
    *,
    total_local_cases: int,
    policy: DiseaseAlertPolicy,
) -> float:
    if total_local_cases >= max(2, policy.critical_case_threshold):
        return 1.0
    if total_local_cases >= policy.cluster_case_threshold:
        return 0.7
    if total_local_cases > 1:
        return 0.35
    return 0.15 if policy.high_priority else 0.05


def _trend_factor(
    *,
    trend: TrendSnapshot,
    policy: DiseaseAlertPolicy,
) -> float:
    if trend.previous_count == 0:
        if trend.current_count >= max(2, policy.cluster_case_threshold):
            return 0.8
        if trend.current_count > 0:
            return 0.2
        return 0.0

    if trend.surge_ratio >= 3.0 or trend.growth_rate >= 2.0:
        return 1.0
    if trend.surge_ratio >= 2.0 or trend.growth_rate >= 1.0:
        return 0.75
    if trend.growth_rate >= 0.5:
        return 0.45
    if trend.current_count > 0:
        return 0.1
    return 0.0


def _is_meaningful_growth(
    *,
    trend: TrendSnapshot,
    policy: DiseaseAlertPolicy,
) -> bool:
    if trend.previous_count == 0:
        return trend.current_count >= max(2, policy.cluster_case_threshold)

    return trend.growth_rate >= 0.5 or trend.surge_ratio >= 1.5


def _determine_alert_level(
    *,
    score: float,
    total_local_cases: int,
    trend: TrendSnapshot,
    policy: DiseaseAlertPolicy,
) -> AlertLevel:
    if policy.rare_disease and trend.current_count >= 1:
        return "critical"

    if total_local_cases >= policy.critical_case_threshold and (
        policy.high_priority
        or trend.current_count >= policy.critical_case_threshold
        or trend.surge_ratio >= 2.0
    ):
        return "critical"

    if score >= 80:
        return "critical"

    if policy.high_priority and total_local_cases >= policy.cluster_case_threshold:
        return "high"

    if total_local_cases >= policy.cluster_case_threshold and _is_meaningful_growth(
        trend=trend,
        policy=policy,
    ):
        return "high"

    if score >= 55:
        return "high"

    if score >= 35 or _is_meaningful_growth(trend=trend, policy=policy) or total_local_cases > 1:
        return "medium"

    if score > 0:
        return "low"

    return "no_alert"


def _build_reasons(
    *,
    nearby_case_count: int,
    total_local_cases: int,
    trend: TrendSnapshot,
    policy: DiseaseAlertPolicy,
) -> list[str]:
    reasons: list[str] = []

    if policy.rare_disease:
        reasons.append("Rare-disease policy is active.")
    if policy.high_priority:
        reasons.append("High-priority disease policy is active.")

    if nearby_case_count > 0:
        reasons.append(
            f"{nearby_case_count} nearby case(s) matched within {policy.radius_km} km."
        )

    if total_local_cases >= policy.cluster_case_threshold:
        reasons.append(
            f"Local case count reached the cluster threshold ({policy.cluster_case_threshold})."
        )

    if total_local_cases >= policy.critical_case_threshold:
        reasons.append(
            f"Local case count reached the critical threshold ({policy.critical_case_threshold})."
        )

    if trend.previous_count == 0 and trend.current_count > 0:
        reasons.append("New activity detected without a previous baseline.")
    elif trend.surge_ratio >= 2.0:
        reasons.append(f"Temporal surge detected with ratio {trend.surge_ratio}.")
    elif trend.growth_rate >= 0.5:
        reasons.append(f"Case growth rate reached {trend.growth_rate}.")

    return reasons


def evaluate_visit_outbreak(
    *,
    context: VisitOutbreakContext,
    policy: DiseaseAlertPolicy,
) -> OutbreakAnalysis:
    """
    Evaluate whether a new visit should trigger an outbreak alert.

    The engine combines:
    - disease policy weights
    - nearby case density
    - temporal trend between current and previous windows

    It returns a pure decision object and does not write to the database.
    """
    region_filter = policy.region_type
    current_start, current_end, previous_start, previous_end = _build_time_windows(
        diagnosis_date=context.diagnosis_date,
        lookback_days=policy.lookback_days,
        baseline_window_days=policy.baseline_window_days,
    )
    current_window_visits = Visit.objects.filter(
        disease_id=context.disease_id,
        diagnosis_date__gte=current_start,
        diagnosis_date__lte=current_end,
    )
    trend = build_trend_snapshot(
        disease_id=context.disease_id,
        current_start=current_start,
        current_end=current_end,
        previous_start=previous_start,
        previous_end=previous_end,
        region_type=region_filter,
    )

    context_is_active = True
    if context.visit_id:
        context_is_active = is_active_visit_in_queryset(
            visit_id=context.visit_id,
            queryset=current_window_visits,
        )

    if not context_is_active:
        return OutbreakAnalysis(
            alert_level="no_alert",
            score=0.0,
            reasons=["Latest patient+disease visit is not infected."],
            nearby_case_count=0,
            matched_case_ids=[],
            trend=trend,
            should_create_report=False,
            should_create_cluster=False,
            metadata={
                "local_case_count": 0,
                "context_active": False,
                "region_filter": region_filter,
                "score_breakdown": {
                    "severity_points": 0.0,
                    "density_points": 0.0,
                    "trend_points": 0.0,
                    "rarity_points": 0.0,
                },
                "current_window": {
                    "start": current_start.isoformat(),
                    "end": current_end.isoformat(),
                },
                "previous_window": {
                    "start": previous_start.isoformat(),
                    "end": previous_end.isoformat(),
                },
            },
        )

    nearby_cases = find_nearby_cases(
        context=context,
        radius_km=policy.radius_km,
        lookback_days=policy.lookback_days,
        region_type=region_filter,
    )
    nearby_case_count = len(nearby_cases)
    total_local_cases = nearby_case_count + 1

    severity_points = 8 * policy.severity_weight + (6 if policy.high_priority else 0)
    density_points = 20 * policy.density_weight * _density_factor(
        total_local_cases=total_local_cases,
        policy=policy,
    )
    trend_points = 16 * policy.trend_weight * _trend_factor(
        trend=trend,
        policy=policy,
    )
    rarity_points = 20 * policy.rarity_weight if policy.rare_disease else 0

    raw_score = severity_points + density_points + trend_points + rarity_points
    score = round(min(raw_score, 100.0), 2)

    alert_level = _determine_alert_level(
        score=score,
        total_local_cases=total_local_cases,
        trend=trend,
        policy=policy,
    )
    reasons = _build_reasons(
        nearby_case_count=nearby_case_count,
        total_local_cases=total_local_cases,
        trend=trend,
        policy=policy,
    )

    should_create_report = alert_level in {"medium", "high", "critical"}
    should_create_cluster = (
        alert_level in {"high", "critical"}
        and total_local_cases >= max(2, policy.cluster_case_threshold)
    )

    return OutbreakAnalysis(
        alert_level=alert_level,
        score=score,
        reasons=reasons,
        nearby_case_count=nearby_case_count,
        matched_case_ids=[case.visit_id for case in nearby_cases],
        trend=trend,
        should_create_report=should_create_report,
        should_create_cluster=should_create_cluster,
        metadata={
            "local_case_count": total_local_cases,
            "context_active": True,
            "region_filter": region_filter,
            "score_breakdown": {
                "severity_points": round(severity_points, 2),
                "density_points": round(density_points, 2),
                "trend_points": round(trend_points, 2),
                "rarity_points": round(rarity_points, 2),
            },
            "current_window": {
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
            },
            "previous_window": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
            },
        },
    )
