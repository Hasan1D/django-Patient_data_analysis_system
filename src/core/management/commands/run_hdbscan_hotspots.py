from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from core.services.hdbscan_hotspots import (
    detect_hdbscan_clusters,
    persist_hdbscan_clusters,
    serialize_hdbscan_clusters,
)


class Command(BaseCommand):
    help = "Detect disease hotspots using HDBSCAN spatial clustering."

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
        parser.add_argument("--min-cluster-size", type=int, default=3, help="Minimum cluster size for HDBSCAN.")
        parser.add_argument("--min-samples", type=int, help="Optional HDBSCAN min_samples value.")
        parser.add_argument(
            "--cluster-selection-method",
            default="eom",
            choices=("eom", "leaf"),
            help="HDBSCAN cluster selection method.",
        )
        parser.add_argument(
            "--allow-single-cluster",
            action="store_true",
            help="Allow HDBSCAN to return one global cluster.",
        )
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

        try:
            result = detect_hdbscan_clusters(
                disease_id=options.get("disease_id"),
                lookback_days=options.get("lookback_days"),
                date_from=date_from,
                date_to=date_to,
                region_type=options.get("region_type"),
                min_cluster_size=options.get("min_cluster_size"),
                min_samples=options.get("min_samples"),
                cluster_selection_method=options.get("cluster_selection_method"),
                allow_single_cluster=options.get("allow_single_cluster"),
                point_mode=options.get("point_mode"),
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise CommandError(f"HDBSCAN dependencies are not installed: {exc}") from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        persisted_ids: list[int] = []
        if options.get("persist"):
            persisted_ids = persist_hdbscan_clusters(clusters=result.clusters)

        self.stdout.write(
            self.style.SUCCESS(
                f"Detected {len(result.clusters)} HDBSCAN cluster candidate(s). noise_count={result.noise_count}"
            )
        )
        if persisted_ids:
            self.stdout.write(f"Persisted cluster ids: {persisted_ids}")
        for cluster in serialize_hdbscan_clusters(result.clusters):
            self.stdout.write(str(cluster))
