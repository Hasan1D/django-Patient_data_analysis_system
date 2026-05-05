from django.db.models import Q
from django.utils.dateparse import parse_date

from core.models import Visit
from core.services.active_cases import active_visits_queryset


def _report_alert_level(*, risk_score: float, current_case_count: int, surge_ratio: float) -> str:
    if risk_score >= 80 or current_case_count >= 6 or surge_ratio >= 2.5:
        return "critical"
    if risk_score >= 45 or current_case_count >= 3 or surge_ratio >= 1.5:
        return "high"
    if risk_score >= 20 or current_case_count >= 1:
        return "medium"
    return "low"


def _resolve_report_dates(*, visits, date_from: str | None, date_to: str | None):
    first_visit = visits.order_by("diagnosis_date").first()
    last_visit = visits.order_by("-diagnosis_date").first()

    if first_visit is None or last_visit is None:
        return None, None

    analysis_period_start = parse_date(date_from) if date_from else first_visit.diagnosis_date
    analysis_period_end = parse_date(date_to) if date_to else last_visit.diagnosis_date

    if analysis_period_start is None or analysis_period_end is None:
        return None, None

    return analysis_period_start, analysis_period_end


def _build_report_reasons(*, disease, region_type: str | None, current_case_count: int, trend) -> list[str]:
    reasons = [
        f"Report generated for disease {disease.name}.",
        f"Current period captured {current_case_count} case(s).",
    ]

    if region_type:
        reasons.append(f"Region type filter applied: {region_type}.")
    if trend.previous_count == 0 and trend.current_count > 0:
        reasons.append("Current period has no previous baseline cases.")
    elif trend.surge_ratio >= 1.5:
        reasons.append(f"Temporal surge ratio reached {trend.surge_ratio}.")
    elif trend.growth_rate > 0:
        reasons.append(f"Growth rate reached {trend.growth_rate}.")

    return reasons


def _build_generated_report_kwargs(*, disease, analysis_period_start, analysis_period_end, region_type: str | None, trend):
    risk_score = trend.current_count * disease.infection_score * disease.risk_level
    alert_level = _report_alert_level(
        risk_score=risk_score,
        current_case_count=trend.current_count,
        surge_ratio=trend.surge_ratio,
    )
    summary = (
        f"Disease {disease.name} recorded {trend.current_count} case(s) "
        f"between {analysis_period_start} and {analysis_period_end} "
        f"with alert level {alert_level}."
    )

    return {
        "disease": disease,
        "trigger_visit": None,
        "analysis_period_start": analysis_period_start,
        "analysis_period_end": analysis_period_end,
        "alert_level": alert_level,
        "summary": summary,
        "risk_score": risk_score,
        "nearby_case_count": 0,
        "current_case_count": trend.current_count,
        "previous_case_count": trend.previous_count,
        "growth_rate": trend.growth_rate,
        "surge_ratio": trend.surge_ratio,
        "reasons": _build_report_reasons(
            disease=disease,
            region_type=region_type,
            current_case_count=trend.current_count,
            trend=trend,
        ),
        "status": "new",
    }


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _active_alerts_queryset(queryset):
    active_trigger_visit_ids = active_visits_queryset(Visit.objects.all()).values("id")
    return queryset.filter(
        status__in=("new", "reviewed"),
        alert_level__in=("medium", "high", "critical"),
    ).filter(Q(trigger_visit__isnull=True) | Q(trigger_visit_id__in=active_trigger_visit_ids))


def _active_hotspots_queryset(queryset):
    return queryset.filter(risk_level__gte=2)
