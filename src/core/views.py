#from django.shortcuts import render
from rest_framework import viewsets
# هدول مشان الحماية يعني النفوذ لكل مستخدم شو بيقدر يعمل
from rest_framework.permissions import IsAuthenticated
from .permissions import IsDoctorOrAdmin
from .permissions import IsAdminOnly

from .models import Patient , Doctor , Hospital ,Disease , Visit , GeoData , GeoCluster , Report
from .serializers import PatientSerializer
from .serializers import DoctorSerializer
from .serializers import HospitalSerializer
from .serializers import DiseaseSerializer
from .serializers import VisitSerializer
from .serializers import GeoDataSerializer
from .serializers import GeoClusterSerializer
from .serializers import ReportSerializer

from django_filters.rest_framework import DjangoFilterBackend
from .filters import VisitFilter , GeoDataFilter

#endpoint
from django.db.models import Count
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Count # من اجل توليد التقارير
from django.shortcuts import get_object_or_404

# Create your views here.

class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    permission_classes = [IsDoctorOrAdmin]
    
class DoctorViewSet(viewsets.ModelViewSet):
    queryset = Doctor.objects.all()
    serializer_class = DoctorSerializer
    
class HospitalViewSet(viewsets.ModelViewSet):
    queryset = Hospital.objects.all()
    serializer_class = HospitalSerializer
    permission_classes = [IsAuthenticated]

class DiseaseViewSet(viewsets.ModelViewSet):
    queryset = Disease.objects.all()
    serializer_class = DiseaseSerializer
    permission_classes = [IsAuthenticated]
    
class VisitViewSet(viewsets.ModelViewSet):
    queryset = Visit.objects.all()
    serializer_class = VisitSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_class = VisitFilter
    #permission_classes = [IsDoctorOrAdmin]
    
    #endpoint
    # 1️⃣ عدد الحالات لكل مرض
    @action(detail=False, methods=["get"])
    def cases_by_disease(self, request):

        visits = VisitFilter(
            request.GET,
            queryset=Visit.objects.all()
        ).qs
        qs = (
            visits
            .values(
                "disease__id",
                "disease__name"
            )
            .annotate(
                total_cases=Count("id")
            )
            .order_by("-total_cases")
        )

        return Response(qs) 
    
    # 2️⃣ عدد الحالات عبر الزمن
    @action(detail=False, methods=["get"])
    def cases_over_time(self, request):
        
        visits = VisitFilter(
            request.GET,
            queryset=Visit.objects.all()
        ).qs
        qs = (
            Visit.objects
            .values(
                "diagnosis_date"
            )
            .annotate(
                total_cases=Count("id")
            )
            .order_by("diagnosis_date")
        )

        return Response(qs)

    # 3️⃣ عدد الحالات لكل طبيب
    @action(detail=False, methods=["get"])
    def cases_by_doctor(self, request):
        
        visits = VisitFilter(
            request.GET,
            queryset=Visit.objects.all()
        ).qs
        qs = (
            Visit.objects
            .values(
                "doctor__id",
                "doctor__user__username"
            )
            .annotate(
                total_cases=Count("id")
            )
            .order_by("-total_cases")
        )

        return Response(qs)

    # 4️⃣ عدد الحالات حسب نوع المنطقة
    @action(detail=False, methods=["get"])
    def cases_by_region_type(self, request):

        visits = VisitFilter(
            request.GET,
            queryset=Visit.objects.all()
        ).qs

        geodata = GeoData.objects.filter(
            visit__in=visits
        )

        geodata = GeoDataFilter(
            request.GET,
            queryset=geodata
        ).qs

        qs = (
            GeoData.objects
            .values(
                "region_type"
            )
            .annotate(
                total_cases=Count("visit")
            )
            .order_by("-total_cases")
        )

        return Response(qs)

    # 5️⃣ المرض + نوع المنطقة
    @action(detail=False, methods=["get"])
    def disease_region_matrix(self, request):

        visits = VisitFilter(
            request.GET,
            queryset=Visit.objects.all()
        ).qs
        
        geodata = GeoData.objects.filter(
            visit__in=visits
        )
        geodata = GeoDataFilter(
            request.GET,
            queryset=geodata
        ).qs
        
        qs = (
            GeoData.objects
            .values(
                "visit__disease__name",
                "region_type"
            )
            .annotate(
                total_cases=Count("visit")
            )
            .order_by("-total_cases")
        )

        return Response(qs)

    
class GeoDataViewSet(viewsets.ModelViewSet):
    queryset = GeoData.objects.all()
    serializer_class = GeoDataSerializer
    permission_classes = [IsDoctorOrAdmin]
    
class GeoClusterViewSet(viewsets.ModelViewSet):
    queryset = GeoCluster.objects.all()
    serializer_class = GeoClusterSerializer
    permission_classes = [IsAdminOnly]
    
class ReportViewSet(viewsets.ModelViewSet):

    queryset = Report.objects.all()

    serializer_class = ReportSerializer

    # توليد تقرير جديد
    @action(detail=False, methods=["post","get"])
    def generate(self, request):

        disease_id = request.data.get("disease")
        # إذا لم يتم إرسال id أصلاً
        if not disease_id:
            return Response({"error": "Please provide a disease ID"}, status=400)
        # محاولة جلب المرض أو إرجاع 404 بشكل نظيف
        disease = get_object_or_404(Disease, id=disease_id)
        
        region_type = request.data.get("region_type")
        date_from = request.data.get("date_from")
        date_to = request.data.get("date_to")

        visits = Visit.objects.all()

        # فلترة حسب المرض
        if disease_id:
            visits = visits.filter(
                disease_id=disease_id
            )

        # فلترة زمنية
        if date_from:
            visits = visits.filter(
                diagnosis_date__gte=date_from
            )
        if date_to:
            visits = visits.filter(
                diagnosis_date__lte=date_to
            )

        # ربط مع GeoData
        geodata = GeoData.objects.filter(
            visit__in=visits
        )

        # فلترة نوع المنطقة
        if region_type:
            geodata = geodata.filter(
                region_type=region_type
            )

        # حساب عدد الحالات
        total_cases = geodata.count()
        if total_cases == 0:
            return Response(
                {"error": "no data"},
                status=400
            )

        # معلومات المرض
        disease = Disease.objects.get(
            id=disease_id
        )

        # حساب risk score
        risk_score = (
            total_cases
            * disease.infection_score
            * disease.risk_level
        )

        # نص التقرير
        summary = f"""
Disease: {disease.name}

Region type: {region_type}

Total cases: {total_cases}

Calculated risk score: {risk_score}
"""

        # placeholder لرابط الخريطة
        spread_map_url = "http://example.com/map"
        
        report = Report.objects.create(
            region="dynamic region",
            region_type=region_type,
            disease=disease,
            summary=summary,
            risk_score=risk_score,
            spread_map_url=spread_map_url
        )
        
        serializer = ReportSerializer(report)
        return Response(serializer.data)

