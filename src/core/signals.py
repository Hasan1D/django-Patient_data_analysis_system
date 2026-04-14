import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import GeoData
from .services.monitoring_service import run_monitoring_for_geodata


logger = logging.getLogger(__name__)


@receiver(post_save, sender=GeoData)
def trigger_monitoring_after_geodata_save(sender, instance: GeoData, **kwargs):
    def _run_monitoring():
        try:
            run_monitoring_for_geodata(geodata=instance)
        except Exception:  # pragma: no cover
            logger.exception("Automatic outbreak monitoring failed for GeoData %s", instance.id)

    transaction.on_commit(_run_monitoring)
