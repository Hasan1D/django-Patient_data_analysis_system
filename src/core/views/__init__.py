from .auth import (
    RegisterView,
    RegistrationHospitalListView,
    ResendEmailVerificationView,
    VerifyEmailView,
)
from .diseases import DiseaseViewSet, LabTestViewSet
from .geo import GeoClusterViewSet, GeoDataViewSet
from .maps import ActiveMapCasesView, HistoricalMapCasesView
from .patients import PatientViewSet
from .reports import ReportViewSet
from .users import DoctorViewSet, HospitalViewSet, UserViewSet
from .visits import VisitViewSet

__all__ = [
    "RegisterView",
    "RegistrationHospitalListView",
    "ResendEmailVerificationView",
    "VerifyEmailView",
    "DiseaseViewSet",
    "LabTestViewSet",
    "GeoClusterViewSet",
    "GeoDataViewSet",
    "ActiveMapCasesView",
    "HistoricalMapCasesView",
    "PatientViewSet",
    "ReportViewSet",
    "DoctorViewSet",
    "HospitalViewSet",
    "UserViewSet",
    "VisitViewSet",
]
