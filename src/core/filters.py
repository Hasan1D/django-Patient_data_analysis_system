'''
 ماذا أصبح لدينا الآن؟

API أصبح يدعم:

temporal analysis
geographic filtering
disease segmentation
doctor activity tracking

وهذا يمهد لـ:

Phase 3
Case counting
Spread patterns
Risk scoring
'''
import django_filters
from .models import Visit
from .models import GeoData 


class VisitFilter(django_filters.FilterSet):

    disease = django_filters.NumberFilter(field_name="disease_id")

    doctor = django_filters.NumberFilter(field_name="doctor_id")

    patient = django_filters.NumberFilter(field_name="patient_id")


    date_from = django_filters.DateFilter(
        field_name="diagnosis_date",
        lookup_expr='gte'
    )

    date_to = django_filters.DateFilter(
        field_name="diagnosis_date",
        lookup_expr='lte'
    )

    class Meta:
        model = Visit

        fields = [
            "disease",
            "doctor",
            "patient",
        ]
        
        
class GeoDataFilter(django_filters.FilterSet):

    region_type = django_filters.CharFilter(
        field_name="region_type"
    )

    class Meta:

        model = GeoData

        fields = ["region_type"]