
from rest_framework import serializers
from .models import Patient
from .models import Doctor
from .models import Hospital
from .models import Disease
from .models import Visit
from .models import GeoData
from .models import GeoCluster
from .models import Report


#
class PatientSerializer(serializers.ModelSerializer):
    class Meta :
        model = Patient
        fields = '__all__'
        
class DoctorSerializer(serializers.ModelSerializer):
    class Meta :
        model = Doctor
        fields = '__all__'

class HospitalSerializer(serializers.ModelSerializer):
    class Meta :
        model = Hospital
        fields = '__all__'

class DiseaseSerializer(serializers.ModelSerializer):
    class Meta :
        model = Disease
        fields = '__all__'
        
class VisitSerializer(serializers.ModelSerializer):
    class Meta :
        model = Visit
        fields = '__all__'
        
class GeoDataSerializer(serializers.ModelSerializer):
    class Meta :
        model = GeoData
        fields = '__all__'
        
class GeoClusterSerializer(serializers.ModelSerializer):
    class Meta :
        model = GeoCluster
        fields = '__all__'

class ReportSerializer(serializers.ModelSerializer):
    disease_name = serializers.CharField(
        source="disease.name",
        read_only=True
    )
    class Meta:
        model = Report
        fields = "__all__"