"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path , include
from rest_framework.routers import DefaultRouter
from core.views import (
    RegisterView,
    RegistrationHospitalListView,
    PatientViewSet , UserViewSet , DoctorViewSet , HospitalViewSet ,
    DiseaseViewSet ,VisitViewSet , GeoDataViewSet , 
    GeoClusterViewSet , ReportViewSet , LabTestViewSet,
    ResendEmailVerificationView,
    VerifyEmailView,
)

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)


router = DefaultRouter()
router.register(r'patients' , PatientViewSet)
router.register(r'users' , UserViewSet)
router.register(r'doctors' , DoctorViewSet)
router.register(r'hospitals' , HospitalViewSet)
router.register(r'diseases' , DiseaseViewSet)
router.register(r'visits' , VisitViewSet)
router.register(r'geodata' , GeoDataViewSet)
router.register(r'geoclusters' , GeoClusterViewSet)
router.register(r'reports', ReportViewSet)
router.register(r'lab-tests', LabTestViewSet)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include(router.urls)),  
]

urlpatterns += [
    path('api/auth/register/' , RegisterView.as_view() , name='auth-register'),
    path('api/auth/registration-hospitals/' , RegistrationHospitalListView.as_view() , name='auth-registration-hospitals'),
    path('api/auth/verify-email/' , VerifyEmailView.as_view() , name='auth-verify-email'),
    path('api/auth/resend-verification/' , ResendEmailVerificationView.as_view() , name='auth-resend-verification'),
    path('api/token/' , TokenObtainPairView.as_view() , name='token_obtion_pair'),
    path('api/token/refresh/' , TokenRefreshView.as_view() , name='token_refresh'),
]
