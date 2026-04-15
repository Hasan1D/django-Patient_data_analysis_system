from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.services.dataset_export import export_ai_datasets


class Command(BaseCommand):
    help = "Export AI-ready datasets from the current operational data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            required=True,
            help="Directory where the generated dataset CSV files will be written.",
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"]).expanduser()
        if output_dir.exists() and not output_dir.is_dir():
            raise CommandError("output-dir must be a directory path.")

        export_result = export_ai_datasets(output_dir=output_dir)
        self.stdout.write(
            self.style.SUCCESS(
                "Exported AI datasets successfully. "
                f"visit_level_count={export_result['visit_level_count']}, "
                f"disease_region_day_count={export_result['disease_region_day_count']}"
            )
        )
        self.stdout.write(f"visit_level_path={export_result['visit_level_path']}")
        self.stdout.write(f"disease_region_day_path={export_result['disease_region_day_path']}")
