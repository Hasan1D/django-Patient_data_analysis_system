from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db import transaction

from .models import *
from .services.account_service import (
    approve_user_account,
    create_linked_doctor_for_user,
    update_user_activation_state,
)
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
class UserAccountAdminCreationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(label="Password confirmation", widget=forms.PasswordInput)
    specialization = forms.CharField(required=False)
    hospital = forms.ModelChoiceField(queryset=Hospital.objects.all(), required=False)

    class Meta:
        model = User
        fields = (
            "username",
            "real_name",
            "phon_number",
            "email",
            "role",
            "is_staff",
            "is_active",
            "email_verified",
            "admin_approved",
        )

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        role = cleaned_data.get("role")
        specialization = cleaned_data.get("specialization") or ""
        hospital = cleaned_data.get("hospital")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Passwords do not match.")

        if role == User.ROLE_DOCTOR:
            if not specialization.strip():
                self.add_error("specialization", "specialization is required for doctor users.")
            if hospital is None:
                self.add_error("hospital", "hospital is required for doctor users.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if user.is_active:
            user.email_verified = True
            user.admin_approved = True
        if commit:
            user.save()
        return user


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    model = User
    add_form = UserAccountAdminCreationForm

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Personal Info', {'fields': ('real_name', 'phon_number', 'email')}),
        ('Permissions', {
            'fields': (
                'role',
                'is_active',
                'email_verified',
                'admin_approved',
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
                'specialization',
                'hospital',
                'password1',
                'password2',
                'is_staff',
                'is_active',
                'email_verified',
                'admin_approved',
            ),
        }),
    )

    list_display = (
        'username',
        'real_name',
        'role',
        'is_staff',
        'is_active',
        'email_verified',
        'admin_approved',
    )
    list_filter = ('role', 'is_active', 'email_verified', 'admin_approved', 'is_staff')
    search_fields = ('username', 'real_name', 'email')
    ordering = ('username',)
    actions = ('approve_doctor_accounts',)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if change:
                if obj.role == User.ROLE_DOCTOR:
                    update_user_activation_state(obj)
                return

            if obj.role == User.ROLE_DOCTOR:
                create_linked_doctor_for_user(
                    user=obj,
                    specialization=form.cleaned_data["specialization"],
                    hospital=form.cleaned_data["hospital"],
                )
                update_user_activation_state(obj)

    @admin.action(description="Approve selected doctor accounts")
    def approve_doctor_accounts(self, request, queryset):
        approved_count = 0
        skipped_count = 0
        for user in queryset.filter(role=User.ROLE_DOCTOR):
            if not Doctor.objects.filter(user=user).exists():
                skipped_count += 1
                continue
            approve_user_account(user)
            approved_count += 1

        if approved_count:
            self.message_user(
                request,
                f"Approved {approved_count} doctor account(s).",
                messages.SUCCESS,
            )
        if skipped_count:
            self.message_user(
                request,
                f"Skipped {skipped_count} doctor account(s) without linked Doctor records.",
                messages.WARNING,
            )


@admin.register(EmailVerificationCode)
class EmailVerificationCodeAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'created_at', 'expires_at', 'attempts')
    search_fields = ('user__username', 'user__email')
    readonly_fields = ('code_hash', 'created_at')


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
    list_display = ('id' , 'patient' , 'doctor' , 'disease' , 'diagnose', 'diagnosis_date' , 'status')
    search_fields = ('patient__name' , 'doctor__user__username', 'diagnose')
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
