from datetime import date

from core.models import Visit

from .contracts import TrendSnapshot


def count_cases_for_window(
    *,
    disease_id: int,
    start_date: date,
    end_date: date,
    region_type: str | None = None,
) -> int:
    """
    Count matching cases inside a time window.

    Counts distinct visits to avoid over-counting when a visit has more than one
    geo record.
    """
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date.")

    visits = Visit.objects.filter(
        disease_id=disease_id,
        diagnosis_date__gte=start_date,
        diagnosis_date__lte=end_date,
    )

    if region_type:
        visits = visits.filter(geodata__region_type=region_type).distinct()

    return visits.count()


def build_trend_snapshot(
    *,
    disease_id: int,
    current_start: date,
    current_end: date,
    previous_start: date,
    previous_end: date,
    region_type: str | None = None,
) -> TrendSnapshot:
    """
    Compare current and previous windows for the same disease.

    growth_rate is the relative change against the previous window when a
    baseline exists. surge_ratio highlights how many times the current window
    exceeds the previous one.
    """
    if current_start > current_end:
        raise ValueError("current_start cannot be after current_end.")
    if previous_start > previous_end:
        raise ValueError("previous_start cannot be after previous_end.")

    current_count = count_cases_for_window(
        disease_id=disease_id,
        start_date=current_start,
        end_date=current_end,
        region_type=region_type,
    )
    previous_count = count_cases_for_window(
        disease_id=disease_id,
        start_date=previous_start,
        end_date=previous_end,
        region_type=region_type,
    )

    if previous_count > 0:
        growth_rate = (current_count - previous_count) / previous_count
        surge_ratio = current_count / previous_count
    elif current_count > 0:
        growth_rate = 1.0
        surge_ratio = float(current_count)
    else:
        growth_rate = 0.0
        surge_ratio = 0.0

    return TrendSnapshot(
        current_count=current_count,
        previous_count=previous_count,
        growth_rate=round(growth_rate, 4),
        surge_ratio=round(surge_ratio, 4),
    )
