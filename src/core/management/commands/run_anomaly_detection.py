from django.core.management.base import BaseCommand, CommandError

from core.services.anomaly_detection import detect_temporal_anomalies, serialize_anomaly_candidates


class Command(BaseCommand):
    help = "Detect temporal disease anomalies from daily case activity."

    def add_arguments(self, parser):
        parser.add_argument("--disease-id", type=int, help="Optional disease id filter.")
        parser.add_argument("--lookback-days", type=int, default=30, help="Recent date window to evaluate.")
        parser.add_argument("--baseline-window-days", type=int, default=7, help="Historical baseline window length.")
        parser.add_argument("--region-type", help="Optional region_type filter.")

    def handle(self, *args, **options):
        try:
            candidates = detect_temporal_anomalies(
                disease_id=options.get("disease_id"),
                lookback_days=options.get("lookback_days"),
                baseline_window_days=options.get("baseline_window_days"),
                region_type=options.get("region_type"),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Detected {len(candidates)} anomaly candidate(s)."
            )
        )
        for candidate in serialize_anomaly_candidates(candidates):
            self.stdout.write(str(candidate))
