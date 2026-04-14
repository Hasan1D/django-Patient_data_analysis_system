from django.contrib import admin
from .models import *
from django.contrib.auth.admin import UserAdmin
# Register your models here.

'''
admin.site.register(User)
admin.site.register(Hospital)
admin.site.register(Doctor)
admin.site.register(Patient)
admin.site.register(Disease)
admin.site.register(Visit)
admin.site.register(GeoData)
admin.site.register(GeoCluster)
admin.site.register(Report)
admin.site.register(MedicalHistory)
admin.site.register(Vaccine)
admin.site.register(Allergy)
admin.site.register(chronicDisease)
admin.site.register(SurgicalHistory)
'''
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class UserAdmin(UserAdmin):
    model = User

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal Info', {'fields': ('real_name', 'phon_number', 'email')}),
        ('Permissions', {
            'fields': (
                'role',
                'is_active',
                'is_staff',
                'is_superuser',
                'groups',
                'user_permissions',
            )
        }),
        ('Important Dates', {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'username',
                'real_name',
                'phon_number',
                'email',
                'role',
                'password1',
                'password2',
                'is_staff',
                'is_active',
            ),
        }),
    )

    list_display = ('username', 'real_name', 'role', 'is_staff', 'is_active')
    search_fields = ('username', 'real_name', 'email')
    ordering = ('username',)


@admin.register(Hospital)
class HospitalAdmin (admin.ModelAdmin):
    list_display = ('id' , 'name' , 'city' , 'location')
    search_fields = ('name' , 'city')
    
    
@admin.register(Doctor)
class DoctorAdmin (admin.ModelAdmin):
    list_display = ('id' , 'user' , 'specialization' , 'hospital')
    search_fields = ('user__username' , 'specialization')
    list_filter = ('specialization' , 'hospital')
    
    
@admin.register(Patient)
class PatientAdmin (admin.ModelAdmin):
    list_display = ('id' , 'name' , 'national_number' , 'gender' , 'birth_date')
    search_fields = ('name' , 'national_number')
    list_filter = ('gender' ,)
    
    
@admin.register(Disease)
class DiseaseAdmin (admin.ModelAdmin):
    list_display = (
        'id',
        'disease_code',
        'name',
        'type',
        'policy_profile',
        'risk_level',
        'infection_score',
        'high_priority',
        'rare_disease',
        'is_reference',
    )
    search_fields = ('name' , 'disease_code')
    list_filter = ('type' , 'policy_profile', 'risk_level', 'high_priority', 'rare_disease', 'is_reference', 'source_name')
    

@admin.register(Visit)
class visitAdmin (admin.ModelAdmin):
    list_display = ('id' , 'patient' , 'doctor' , 'disease' , 'diagnosis_date' , 'status')
    search_fields = ('patient__name' , 'doctor__user__username')
    list_filter = ('disease' , 'diagnosis_date' , 'status' )
    
    
@admin.register(GeoData)
class GeoDataAdmin (admin.ModelAdmin):
    list_display = ('id' , 'patient' , 'visit' , 'region_type' , 'latitude' , 'longitude')
    list_filter = ('region_type' ,)
    
    
@admin.register(GeoCluster)
class GeoClusterAdmin (admin.ModelAdmin):
    list_display = ('id' , 'disease' , 'case_count' , 'risk_level' , 'generated_at')
    list_filter = ('risk_level' , 'disease')


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'disease',
        'trigger_visit',
        'alert_level',
        'risk_score',
        'current_case_count',
        'previous_case_count',
        'status',
        'generated_at',
    )
    search_fields = (
        'disease__name',
        'disease__disease_code',
        'trigger_visit__id',
        'summary',
    )
    list_filter = ('alert_level', 'status', 'generated_at')


@admin.register(MedicalHistory)
class MedicalHistoryAdmin (admin.ModelAdmin):
    list_display = ('id' , 'patient' , 'has_surgical_metal_plates')   


@admin.register(Vaccine)
class VaccineAdmin (admin.ModelAdmin):
    list_display = ('id' , 'vaccine_name' , 'date_administered')
    

@admin.register(Allergy)
class AllergyAdmin (admin.ModelAdmin):
    list_display = ('id' , 'allergy_name' , 'severity_level')
    
    
@admin.register(chronicDisease)
class chronicDiseaseAdmin (admin.ModelAdmin):
    list_display = ('id' , 'disease_name' , 'diagnosis_date')
    

@admin.register(SurgicalHistory)
class SurgicalHistoryAdmin (admin.ModelAdmin):
    list_display = ('id' , 'surgery_description' , 'surgery_date' , 'has_metal_plates')


@admin.register(LabTest)
class LabTestAdmin(admin.ModelAdmin):
    list_display = ('id', 'visit', 'test_code', 'test_name', 'test_date')
    search_fields = ('test_code', 'test_name', 'visit__patient__name')
    list_filter = ('test_date',)
