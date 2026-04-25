from datetime import date, timedelta

from core.models import Disease, Report, Visit

from .contracts import OutbreakAnalysis, VisitOutbreakContext


def _parse_analysis_date(value: object, fallback: date) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value)
    return fallback


def _build_report_summary(*, disease_name: str, analysis: OutbreakAnalysis) -> str:
    parts = [
        f"Outbreak analysis for {disease_name} reached alert level {analysis.alert_level}.",
        f"Risk score: {analysis.score}.",
    ]
    if analysis.trend is not None:
        parts.append(
            f"Current vs previous cases: {analysis.trend.current_count} / {analysis.trend.previous_count}."
        )
    if analysis.reasons:
        parts.append(f"Reasons: {' '.join(analysis.reasons)}")
    return " ".join(parts)


ALERT_LEVEL_PRIORITY = {
    "no_alert": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
ACTIVE_REPORT_STATUSES = ("new", "reviewed")


def _normalized_alert_level(alert_level: str) -> str:
    return "low" if alert_level == "no_alert" else alert_level


def _merge_reasons(existing_reasons: list[str], new_reasons: list[str]) -> list[str]:
    merged: list[str] = []
    for reason in [*existing_reasons, *new_reasons]:
        if reason not in merged:
            merged.append(reason)
    return merged


def _find_existing_report(
    *,
    context: VisitOutbreakContext,
    analysis_period_start: date,
    analysis_period_end: date,
) -> Report | None:
    direct_match = Report.objects.filter(trigger_visit_id=context.visit_id).order_by("-generated_at").first()
    if direct_match is not None:
        return direct_match

    return (
        Report.objects.filter(
            disease_id=context.disease_id,
            status__in=ACTIVE_REPORT_STATUSES,
            analysis_period_end__gte=analysis_period_start - timedelta(days=1),
            analysis_period_start__lte=analysis_period_end + timedelta(days=1),
        )
        .order_by("-generated_at")
        .first()
    )


def create_report_from_analysis(
    *,
    context: VisitOutbreakContext,
    analysis: OutbreakAnalysis,
) -> int:
    """
    Persist a report for a completed outbreak analysis.

    Returns the created report id.
    """
    current_window = analysis.metadata.get("current_window", {})
    analysis_period_start = _parse_analysis_date(current_window.get("start"), context.diagnosis_date)
    analysis_period_end = _parse_analysis_date(current_window.get("end"), context.diagnosis_date)

    existing_report = _find_existing_report(
        context=context,
        analysis_period_start=analysis_period_start,
        analysis_period_end=analysis_period_end,
    )
    if not analysis.should_create_report and existing_report is None:
        raise ValueError("This analysis does not require a report.")

    disease = Disease.objects.get(id=context.disease_id)
    trigger_visit = Visit.objects.filter(id=context.visit_id).first()

    trend = analysis.trend
    normalized_alert_level = _normalized_alert_level(analysis.alert_level)
    current_case_count = trend.current_count if trend is not None else analysis.metadata.get("local_case_count", 0)
    previous_case_count = trend.previous_count if trend is not None else 0
    growth_rate = trend.growth_rate if trend is not None else 0.0
    surge_ratio = trend.surge_ratio if trend is not None else 0.0
    summary = _build_report_summary(disease_name=disease.name, analysis=analysis)

    if existing_report is None:
        report = Report.objects.create(
            disease=disease,
            trigger_visit=trigger_visit,
            analysis_period_start=analysis_period_start,
            analysis_period_end=analysis_period_end,
            alert_level=normalized_alert_level,
            summary=summary,
            risk_score=analysis.score,
            nearby_case_count=analysis.nearby_case_count,
            current_case_count=current_case_count,
            previous_case_count=previous_case_count,
            growth_rate=growth_rate,
            surge_ratio=surge_ratio,
            reasons=analysis.reasons,
            status="new" if analysis.should_create_report else "resolved",
        )
        return report.id

    same_trigger_visit = existing_report.trigger_visit_id == context.visit_id
    existing_report.analysis_period_start = min(existing_report.analysis_period_start, analysis_period_start)
    existing_report.analysis_period_end = max(existing_report.analysis_period_end, analysis_period_end)
    existing_report.trigger_visit = existing_report.trigger_visit or trigger_visit
    existing_report.summary = summary
    existing_report.nearby_case_count = max(existing_report.nearby_case_count, analysis.nearby_case_count)
    existing_report.current_case_count = max(existing_report.current_case_count, current_case_count)
    existing_report.previous_case_count = max(existing_report.previous_case_count, previous_case_count)
    existing_report.growth_rate = max(existing_report.growth_rate, growth_rate)
    existing_report.surge_ratio = max(existing_report.surge_ratio, surge_ratio)
    existing_report.reasons = _merge_reasons(existing_report.reasons, analysis.reasons)

    if same_trigger_visit:
        existing_report.alert_level = normalized_alert_level
        existing_report.risk_score = analysis.score
        existing_report.status = "new" if analysis.should_create_report else "resolved"
    else:
        if ALERT_LEVEL_PRIORITY[normalized_alert_level] >= ALERT_LEVEL_PRIORITY[existing_report.alert_level]:
            existing_report.alert_level = normalized_alert_level
            existing_report.risk_score = analysis.score
        else:
            existing_report.risk_score = max(existing_report.risk_score, analysis.score)
        if analysis.should_create_report:
            existing_report.status = "new"
        elif analysis.metadata.get("context_active") is False and current_case_count == 0:
            existing_report.alert_level = normalized_alert_level
            existing_report.risk_score = analysis.score
            existing_report.status = "resolved"

    existing_report.save()
    return existing_report.id
