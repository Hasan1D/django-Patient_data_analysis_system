from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from core.services.dbscan_hotspots import (
    detect_dbscan_clusters,
    persist_dbscan_clusters,
    serialize_dbscan_clusters,
)


class Command(BaseCommand):
    help = "Detect disease hotspots using a DBSCAN-style spatial clustering pass."

    def add_arguments(self, parser):
        parser.add_argument("--disease-id", type=int, help="Optional disease id to limit detection to one disease.")
        parser.add_argument("--lookback-days", type=int, default=14, help="Lookback window when date range is not provided.")
        parser.add_argument("--date-from", help="Optional YYYY-MM-DD lower diagnosis date bound.")
        parser.add_argument("--date-to", help="Optional YYYY-MM-DD upper diagnosis date bound.")
        parser.add_argument("--region-type", help="Optional region_type filter.")
        parser.add_argument(
            "--point-mode",
            choices=("exposure", "case"),
            default="exposure",
            help="exposure uses all active home/work points; case uses one point per visit.",
        )
        parser.add_argument("--eps-km", type=float, default=3.0, help="Neighborhood radius in kilometers.")
        parser.add_argument("--min-samples", type=int, default=2, help="Minimum nearby points required to form a cluster.")
        parser.add_argument(
            "--persist",
            action="store_true",
            help="Persist detected cluster candidates into GeoCluster.",
        )

    def handle(self, *args, **options):
        date_from = parse_date(options["date_from"]) if options.get("date_from") else None
        date_to = parse_date(options["date_to"]) if options.get("date_to") else None
        if options.get("date_from") and date_from is None:
            raise CommandError("date-from must be in YYYY-MM-DD format.")
        if options.get("date_to") and date_to is None:
            raise CommandError("date-to must be in YYYY-MM-DD format.")

        clusters = detect_dbscan_clusters(
            disease_id=options.get("disease_id"),
            lookback_days=options.get("lookback_days"),
            date_from=date_from,
            date_to=date_to,
            region_type=options.get("region_type"),
            eps_km=options.get("eps_km"),
            min_samples=options.get("min_samples"),
            point_mode=options.get("point_mode"),
        )

        persisted_ids: list[int] = []
        if options.get("persist"):
            persisted_ids = persist_dbscan_clusters(clusters=clusters)

        self.stdout.write(
            self.style.SUCCESS(
                f"Detected {len(clusters)} DBSCAN cluster candidate(s)."
            )
        )
        if persisted_ids:
            self.stdout.write(f"Persisted cluster ids: {persisted_ids}")
        for cluster in serialize_dbscan_clusters(clusters):
            self.stdout.write(str(cluster))
