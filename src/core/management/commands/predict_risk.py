from django.core.management.base import BaseCommand, CommandError

from core.models import Visit
from core.services.risk_prediction import predict_visit_risk, serialize_risk_prediction


class Command(BaseCommand):
    help = "Predict supervised risk class for a visit using the trained risk model."

    def add_arguments(self, parser):
        parser.add_argument("--visit-id", type=int, required=True, help="Visit id to predict.")
        parser.add_argument("--model-path", help="Optional model artifact path.")

    def handle(self, *args, **options):
        try:
            visit = Visit.objects.get(id=options["visit_id"])
        except Visit.DoesNotExist as exc:
            raise CommandError("Visit not found.") from exc

        try:
            prediction = predict_visit_risk(
                visit=visit,
                model_path=options.get("model_path"),
            )
        except (FileNotFoundError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Predicted visit risk successfully."))
        self.stdout.write(str(serialize_risk_prediction(prediction)))
