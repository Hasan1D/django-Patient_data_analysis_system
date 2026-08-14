import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import GeoData, MedicalHistory, Patient, Visit
from .services.map_realtime import broadcast_geodata_change, broadcast_visit_change
from .services.monitoring_service import run_monitoring_for_geodata


logger = logging.getLogger(__name__)


@receiver(post_save, sender=GeoData)
def trigger_monitoring_after_geodata_save(sender, instance: GeoData, **kwargs):
    if kwargs.get("raw"):
        return

    def _run_monitoring():
        try:
            run_monitoring_for_geodata(geodata=instance)
        except Exception:  # pragma: no cover
            logger.exception("Automatic outbreak monitoring failed for GeoData %s", instance.id)

    transaction.on_commit(_run_monitoring)


@receiver(post_save, sender=GeoData)
def broadcast_map_event_after_geodata_save(sender, instance: GeoData, created: bool, **kwargs):
    if kwargs.get("raw"):
        return

    def _broadcast_map_event():
        try:
            broadcast_geodata_change(geodata=instance, created=created)
        except Exception:  # pragma: no cover
            logger.exception("Realtime map event failed for GeoData %s", instance.id)

    transaction.on_commit(_broadcast_map_event)


@receiver(post_save, sender=Visit)
def broadcast_map_event_after_visit_save(sender, instance: Visit, created: bool, **kwargs):
    if kwargs.get("raw"):
        return
    if created:
        return

    def _broadcast_map_event():
        try:
            broadcast_visit_change(visit=instance)
        except Exception:  # pragma: no cover
            logger.exception("Realtime map event failed for Visit %s", instance.id)

    transaction.on_commit(_broadcast_map_event)


@receiver(post_save, sender=Patient)
def create_medical_history_after_patient_create(sender, instance: Patient, created: bool, **kwargs):
    if kwargs.get("raw"):
        return
    if created:
        MedicalHistory.objects.get_or_create(patient=instance)
