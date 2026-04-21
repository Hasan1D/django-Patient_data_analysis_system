from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services.risk_prediction import train_baseline_risk_model


class Command(BaseCommand):
    help = "Train the baseline supervised risk prediction model."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-path",
            help="Optional path for the generated risk model JSON artifact.",
        )

    def handle(self, *args, **options):
        output_path = Path(options["output_path"]) if options.get("output_path") else None
        try:
            result = train_baseline_risk_model(output_path=output_path)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        artifact = result["artifact"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Trained risk model successfully. sample_count={artifact['sample_count']}"
            )
        )
        self.stdout.write(f"model_path={result['model_path']}")
        self.stdout.write(f"label_counts={artifact['label_counts']}")
