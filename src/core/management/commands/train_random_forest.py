from django.core.management.base import BaseCommand, CommandError

from core.services.ml.random_forest import train_random_forest_model


class Command(BaseCommand):
    help = "Train the RandomForestClassifier used for alert_level prediction."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-path",
            dest="output_path",
            help="Optional path for the trained joblib model artifact.",
        )
        parser.add_argument(
            "--metadata-path",
            dest="metadata_path",
            help="Optional path for the model metadata JSON artifact.",
        )
        parser.add_argument(
            "--n-estimators",
            type=int,
            default=100,
            help="Number of trees in the random forest.",
        )
        parser.add_argument(
            "--random-state",
            type=int,
            default=42,
            help="Random seed used by scikit-learn.",
        )

    def handle(self, *args, **options):
        try:
            result = train_random_forest_model(
                output_path=options.get("output_path"),
                metadata_path=options.get("metadata_path"),
                n_estimators=options.get("n_estimators"),
                random_state=options.get("random_state"),
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise CommandError(f"Random forest dependencies are not installed: {exc}") from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Trained RandomForestClassifier "
                f"with {result.metadata['sample_count']} sample(s)."
            )
        )
        self.stdout.write(f"Model artifact: {result.model_path}")
        self.stdout.write(f"Metadata artifact: {result.metadata_path}")
