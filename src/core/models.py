from datetime import date

from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.auth.models import AbstractUser

# Create your models here.

class User(AbstractUser) :
    real_name = models.CharField(max_length=255)
    phon_number = models.CharField(max_length=20)
    role = models.CharField(max_length=50)
    
    
    def __str__(self):
        return self.username
    


class Hospital(models.Model):
    name = models.CharField(max_length=255)
    hospital_lat = models.FloatField()
    hospital_long = models.FloatField()
    location = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    
    
    def __str__(self):
        return self.name
    
    
    
class Doctor(models.Model):
    user = models.OneToOneField(User ,on_delete=models.CASCADE)
    specialization = models.CharField(max_length=255)
    hospital = models.ForeignKey(Hospital , on_delete=models.CASCADE)
    
    
    def __str__(self):
        return self.user.username



class Patient(models.Model):
    national_number =models.CharField(max_length=20 , unique=True)
    name = models.CharField(max_length=255)
    birth_date = models.DateField()
    gender = models.CharField(max_length=10)
    residence_lat = models.FloatField()    
    residence_long = models.FloatField()    
    work_lat = models.FloatField()    
    work_long = models.FloatField()
    
    
    def __str__(self):
        return self.name



class Disease(models.Model):
    POLICY_PROFILE_CHOICES = [
        ("general", "General"),
        ("high_priority", "High Priority"),
        ("rare", "Rare"),
        ("cluster_sensitive", "Cluster Sensitive"),
        ("surge_sensitive", "Surge Sensitive"),
        ("environmental_signal", "Environmental Signal"),
    ]

    disease_code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=255)       
    type = models.CharField(max_length=100)       
    transmission_vector = models.CharField(max_length=100)
    symptoms = models.TextField()
    risk_level = models.IntegerField()
    infection_score = models.FloatField()
    source_record_id = models.PositiveIntegerField(null=True, blank=True)
    source_name = models.CharField(max_length=100, blank=True, default="")
    is_reference = models.BooleanField(default=False)
    policy_profile = models.CharField(
        max_length=50,
        choices=POLICY_PROFILE_CHOICES,
        default="general",
    )
    high_priority = models.BooleanField(default=False)
    rare_disease = models.BooleanField(default=False)

    class Meta:
        ordering = ("name",)
    
    
    def __str__(self):
        return self.name



class Visit(models.Model):
    patient = models.ForeignKey(Patient , on_delete=models.CASCADE)   
    doctor = models.ForeignKey(Doctor , on_delete=models.CASCADE)   
    disease = models.ForeignKey(Disease , on_delete=models.CASCADE)
    diagnose = models.TextField(blank=True, default="")
    diagnosis_date = models.DateField()
    status = models.CharField(max_length=100)
    weight = models.FloatField()   
    height = models.FloatField()  
    marital_status = models.CharField(max_length=50)
     
    
    def __str__ (self):
        return f"Visit {self.id} - {self.patient.name}"
    
    
    
class GeoData(models.Model):
    patient = models.ForeignKey(Patient , on_delete=models.CASCADE)
    visit = models.ForeignKey(Visit , on_delete=models.CASCADE)
    latitude = models.FloatField()
    longitude = models.FloatField()
    region_type = models.CharField(max_length=50)
    
    
    def __str__(self):
        return f"{self.patient.name} - {self.region_type}"

    def clean(self):
        super().clean()
        if self.patient_id is not None and self.visit_id is not None and self.patient_id != self.visit.patient_id:
            raise ValidationError({"patient": "patient must match visit.patient."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
    
    
    
class GeoCluster(models.Model):
    center_lat = models.FloatField()
    center_long = models.FloatField()
    radius = models.FloatField()
    disease = models.ForeignKey(Disease , on_delete=models.CASCADE)
    case_count = models.IntegerField()
    risk_level = models.IntegerField()
    generated_at = models.DateTimeField(auto_now_add=True)
    
    
    def __str__(self):
        return f"{self.disease.name} Cluster ({self.case_count} case)"


class Report(models.Model):
    ALERT_LEVEL_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    ]
    STATUS_CHOICES = [
        ("new", "New"),
        ("reviewed", "Reviewed"),
        ("resolved", "Resolved"),
        ("archived", "Archived"),
    ]

    generated_at = models.DateTimeField(auto_now_add=True)
    disease = models.ForeignKey(Disease , on_delete=models.CASCADE, related_name="reports")
    trigger_visit = models.ForeignKey(
        Visit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reports",
    )
    analysis_period_start = models.DateField(default=date.today)
    analysis_period_end = models.DateField(default=date.today)
    alert_level = models.CharField(max_length=20, choices=ALERT_LEVEL_CHOICES, default="low")
    summary = models.TextField()
    risk_score = models.FloatField()
    nearby_case_count = models.PositiveIntegerField(default=0)
    current_case_count = models.PositiveIntegerField(default=0)
    previous_case_count = models.PositiveIntegerField(default=0)
    growth_rate = models.FloatField(default=0)
    surge_ratio = models.FloatField(default=0)
    reasons = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new")

    class Meta:
        ordering = ("-generated_at",)

    def __str__(self):
        return f"Report {self.id} - {self.disease.name} - {self.alert_level}"


class LabTest(models.Model):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE)
    test_code = models.CharField(max_length=100)
    test_name = models.CharField(max_length=255)
    result = models.TextField()
    test_date = models.DateTimeField()
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "LAB_TESTS"

    def __str__(self):
        return f"{self.test_name} - Visit {self.visit_id}"


class MedicalHistory(models.Model):
    patient = models.ForeignKey(Patient , on_delete=models.CASCADE)
    has_surgical_metal_plates = models.BooleanField(default=False)
    
    
    def __str__(self):
        return f"Medical History of {self.patient.name}"
    
    
    
class Vaccine(models.Model):
    history = models.ForeignKey(MedicalHistory , on_delete=models.CASCADE)
    vaccine_name = models.CharField(max_length=255)
    date_administered = models.DateField()
    
    def __str__(self):
        return f"{self.vaccine_name} for {self.history.patient.name}"
    
    
class Allergy(models.Model):
    history = models.ForeignKey(MedicalHistory , on_delete=models.CASCADE)
    allergy_name = models.CharField(max_length=255)
    severity_level = models.CharField(max_length=50)
    
    def __str__(self):
        return f"{self.allergy_name} ({self.severity_level})"
    
    
class chronicDisease(models.Model):
    history = models.ForeignKey(MedicalHistory , on_delete=models.CASCADE)
    disease_name = models.CharField(max_length=255)
    diagnosis_date = models.DateField()
    
    def __str__(self):
        return f"{self.disease_name} for {self.history.patient.name}"
    
    
    
class SurgicalHistory(models.Model):
    history = models.ForeignKey(MedicalHistory , on_delete=models.CASCADE)
    surgery_description = models.CharField(max_length=255)
    surgery_date = models.DateField()
    has_metal_plates = models.BooleanField(default=False)
    
    def __str__(self):
        return f"Surgery for {self.history.patient.name}"
     

    
    
    



    
    
    
