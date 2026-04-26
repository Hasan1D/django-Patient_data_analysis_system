from django.db.models import OuterRef, QuerySet, Subquery

from core.models import GeoData, Visit


ACTIVE_VISIT_STATUS = Visit.STATUS_INFECTED
INACTIVE_VISIT_STATUS = Visit.STATUS_CURED


def latest_visits_per_patient_disease(queryset: QuerySet | None = None) -> QuerySet:
    """
    Return the latest visit for each patient+disease pair inside queryset.

    The queryset is the analysis window. Status is intentionally not inspected
    here so a later cured visit can suppress an earlier infected visit.
    """
    scoped_visits = (queryset if queryset is not None else Visit.objects.all()).order_by()
    scoped_visit_ids = scoped_visits.values("id")
    latest_visit_id = (
        Visit.objects.filter(
            id__in=scoped_visit_ids,
            patient_id=OuterRef("patient_id"),
            disease_id=OuterRef("disease_id"),
        )
        .order_by("-diagnosis_date", "-id")
        .values("id")[:1]
    )
    return scoped_visits.filter(id=Subquery(latest_visit_id)).distinct()


def active_visits_queryset(queryset: QuerySet | None = None) -> QuerySet:
    return latest_visits_per_patient_disease(queryset).filter(status__iexact=ACTIVE_VISIT_STATUS)


def is_active_visit_in_queryset(*, visit_id: int | None, queryset: QuerySet | None = None) -> bool:
    if visit_id is None:
        return False
    return active_visits_queryset(queryset).filter(id=visit_id).exists()


def one_geodata_per_visit_queryset(queryset: QuerySet | None = None) -> QuerySet:
    scoped_geodata = (queryset if queryset is not None else GeoData.objects.all()).order_by()
    scoped_geodata_ids = scoped_geodata.values("id")
    first_geodata_id = (
        GeoData.objects.filter(
            id__in=scoped_geodata_ids,
            visit_id=OuterRef("visit_id"),
        )
        .order_by("id")
        .values("id")[:1]
    )
    return scoped_geodata.filter(id=Subquery(first_geodata_id))


def active_geodata_queryset(
    queryset: QuerySet | None = None,
    *,
    visit_queryset: QuerySet | None = None,
    one_per_visit: bool = False,
) -> QuerySet:
    scoped_geodata = queryset if queryset is not None else GeoData.objects.all()
    scoped_visit_queryset = visit_queryset
    if scoped_visit_queryset is None:
        scoped_visit_queryset = Visit.objects.filter(id__in=scoped_geodata.order_by().values("visit_id"))

    active_visit_ids = active_visits_queryset(scoped_visit_queryset).values("id")
    active_geodata = scoped_geodata.filter(visit_id__in=active_visit_ids)
    if one_per_visit:
        active_geodata = one_geodata_per_visit_queryset(active_geodata)
    return active_geodata
