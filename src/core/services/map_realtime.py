from __future__ import annotations

import logging

from asgiref.sync import async_to_sync

from core.models import GeoData, Visit
from core.services.map_cases import (
    CASE_EVENT_ADDED,
    CASE_EVENT_REMOVED,
    CASE_EVENT_UPDATED,
    active_case_groups_for_case,
    geodata_for_visit_family,
    is_latest_visit_for_patient_disease,
    is_live_active_geodata,
    serialize_map_case,
)


logger = logging.getLogger(__name__)


def broadcast_case_event(*, event_type: str, case: dict) -> None:
    try:
        from channels.layers import get_channel_layer
    except ImportError:  # pragma: no cover
        logger.warning("Django Channels is not installed; map realtime event was skipped.")
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    payload = {
        "type": "map.case.event",
        "payload": {
            "type": event_type,
            "case": case,
        },
    }
    for group_name in active_case_groups_for_case(case):
        async_to_sync(channel_layer.group_send)(group_name, payload)


def broadcast_geodata_change(*, geodata: GeoData, created: bool = False) -> None:
    geodata = (
        GeoData.objects.select_related("visit__disease")
        .get(id=geodata.id)
    )
    case = serialize_map_case(geodata)
    if is_live_active_geodata(geodata):
        event_type = CASE_EVENT_ADDED if created else CASE_EVENT_UPDATED
    else:
        event_type = CASE_EVENT_REMOVED

    broadcast_case_event(event_type=event_type, case=case)
    broadcast_removed_previous_cases(visit=geodata.visit)


def broadcast_visit_change(*, visit: Visit) -> None:
    visit = Visit.objects.select_related("disease").get(id=visit.id)
    for geodata in GeoData.objects.select_related("visit__disease").filter(visit=visit):
        if is_live_active_geodata(geodata):
            event_type = CASE_EVENT_UPDATED
        else:
            event_type = CASE_EVENT_REMOVED
        broadcast_case_event(
            event_type=event_type,
            case=serialize_map_case(geodata),
        )

    broadcast_removed_previous_cases(visit=visit)


def broadcast_removed_previous_cases(*, visit: Visit) -> None:
    if not is_latest_visit_for_patient_disease(visit):
        return

    old_geodata = geodata_for_visit_family(visit).exclude(visit_id=visit.id)
    for geodata in old_geodata:
        broadcast_case_event(
            event_type=CASE_EVENT_REMOVED,
            case=serialize_map_case(geodata),
        )
