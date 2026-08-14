from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models.functions import Lower
from django.utils import timezone

# Create your models here.

class User(AbstractUser) :
    ROLE_ADMIN = "admin"
    ROLE_DOCTOR = "doctor"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_DOCTOR, "Doctor"),
    ]

    real_name = models.CharField(max_length=255)
    phon_number = models.CharField(max_length=20)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    email_verified = models.BooleanField(default=False)
    admin_approved = models.BooleanField(default=False)

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=["admin", "doctor"]),
                name="core_user_role_admin_or_doctor",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="core_user_unique_email_ci_not_blank",
            ),
        ]
    
    
    def __str__(self):
        return self.username


class EmailVerificationCode(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="email_verification_code",
    )
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Email verification for {self.user.username}"
    


class Hospital(models.Model):
    name = models.CharField(max_length=255)
    hospital_lat = models.FloatField(
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    hospital_long = models.FloatField(
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    location = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(hospital_lat__gte=-90, hospital_lat__lte=90),
                name="core_hospital_lat_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(hospital_long__gte=-180, hospital_long__lte=180),
                name="core_hospital_long_valid",
            ),
        ]
    
    
    def __str__(self):
        return self.name
    
    
    
class Doctor(models.Model):
    user = models.OneToOneField(User ,on_delete=models.CASCADE)
    specialization = models.CharField(max_length=255)
    hospital = models.ForeignKey(Hospital , on_delete=models.CASCADE)
    
    
    def __str__(self):
        return self.user.username



class Patient(models.Model):
    GENDER_MALE = "male"
    GENDER_FEMALE = "female"
    GENDER_CHOICES = [
        (GENDER_MALE, "Male"),
        (GENDER_FEMALE, "Female"),
    ]

    national_number =models.CharField(max_length=20 , unique=True)
    name = models.CharField(max_length=255)
    birth_date = models.DateField()
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES)
    residence_lat = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    residence_long = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    work_lat = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    work_long = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(gender__in=["male", "female"]),
                name="core_patient_gender_male_or_female",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(residence_lat__isnull=True)
                    | models.Q(residence_lat__gte=-90, residence_lat__lte=90)
                ),
                name="core_patient_residence_lat_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(residence_long__isnull=True)
                    | models.Q(residence_long__gte=-180, residence_long__lte=180)
                ),
                name="core_patient_residence_long_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(work_lat__isnull=True)
                    | models.Q(work_lat__gte=-90, work_lat__lte=90)
                ),
                name="core_patient_work_lat_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(work_long__isnull=True)
                    | models.Q(work_long__gte=-180, work_long__lte=180)
                ),
                name="core_patient_work_long_valid",
            ),
        ]
    
    
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
    risk_level = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    infection_score = models.FloatField(validators=[MinValueValidator(0)])
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
        constraints = [
            models.CheckConstraint(
                condition=models.Q(risk_level__gte=0, risk_level__lte=5),
                name="core_disease_risk_level_0_5",
            ),
            models.CheckConstraint(
                condition=models.Q(infection_score__gte=0),
                name="core_disease_infection_score_non_negative",
            ),
        ]
    
    
    def __str__(self):
        return self.name



class Visit(models.Model):
    STATUS_INFECTED = "infected"
    STATUS_CURED = "cured"
    STATUS_CHOICES = [
        (STATUS_INFECTED, "Infected"),
        (STATUS_CURED, "Cured"),
    ]
    MARITAL_STATUS_DIVORCED = "divorced"
    MARITAL_STATUS_SINGLE = "single"
    MARITAL_STATUS_MARRIED = "married"
    MARITAL_STATUS_WIDOW = "widow"
    MARITAL_STATUS_CHOICES = [
        (MARITAL_STATUS_DIVORCED, "Divorced"),
        (MARITAL_STATUS_SINGLE, "Single"),
        (MARITAL_STATUS_MARRIED, "Married"),
        (MARITAL_STATUS_WIDOW, "Widow"),
    ]

    patient = models.ForeignKey(Patient , on_delete=models.CASCADE)   
    doctor = models.ForeignKey(Doctor , on_delete=models.CASCADE)   
    disease = models.ForeignKey(Disease , on_delete=models.CASCADE)
    diagnose = models.TextField(blank=True, default="")
    diagnosis_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    weight = models.FloatField(
        validators=[MinValueValidator(0.1), MaxValueValidator(500)],
    )
    height = models.FloatField(
        validators=[MinValueValidator(30), MaxValueValidator(250)],
    )
    marital_status = models.CharField(max_length=20, choices=MARITAL_STATUS_CHOICES)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["infected", "cured"]),
                name="core_visit_status_infected_or_cured",
            ),
            models.CheckConstraint(
                condition=models.Q(marital_status__in=["divorced", "single", "married", "widow"]),
                name="core_visit_marital_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(weight__gt=0, weight__lte=500),
                name="core_visit_weight_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(height__gte=30, height__lte=250),
                name="core_visit_height_valid",
            ),
        ]
     
    
    def __str__ (self):
        return f"Visit {self.id} - {self.patient.name}"
    
    
    
class GeoData(models.Model):
    REGION_HOME = "home"
    REGION_WORK = "work"
    REGION_TYPE_CHOICES = [
        (REGION_HOME, "Home"),
        (REGION_WORK, "Work"),
    ]

    patient = models.ForeignKey(Patient , on_delete=models.CASCADE)
    visit = models.ForeignKey(Visit , on_delete=models.CASCADE)
    latitude = models.FloatField(
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.FloatField(
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    region_type = models.CharField(max_length=20, choices=REGION_TYPE_CHOICES)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(region_type__in=["home", "work"]),
                name="core_geodata_region_type_home_or_work",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90),
                name="core_geodata_latitude_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180, longitude__lte=180),
                name="core_geodata_longitude_valid",
            ),
        ]
    
    
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
    center_lat = models.FloatField(
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    center_long = models.FloatField(
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    radius = models.FloatField(validators=[MinValueValidator(0)])
    disease = models.ForeignKey(Disease , on_delete=models.CASCADE)
    case_count = models.IntegerField(validators=[MinValueValidator(0)])
    risk_level = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(5)],
    )
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(center_lat__gte=-90, center_lat__lte=90),
                name="core_geocluster_center_lat_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(center_long__gte=-180, center_long__lte=180),
                name="core_geocluster_center_long_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(radius__gte=0),
                name="core_geocluster_radius_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(case_count__gte=0),
                name="core_geocluster_case_count_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(risk_level__gte=0, risk_level__lte=5),
                name="core_geocluster_risk_level_0_5",
            ),
        ]
    
    
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
    patient = models.OneToOneField(Patient , on_delete=models.CASCADE)
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
     

    
    


class SupportTicket(models.Model):
    STATUS_OPEN = 'open'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_RESOLVED = 'resolved'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_RESOLVED, 'Resolved'),
        (STATUS_CLOSED, 'Closed'),
    ]

    PRIORITY_LOW = 'low'
    PRIORITY_MEDIUM = 'medium'
    PRIORITY_HIGH = 'high'
    PRIORITY_URGENT = 'urgent'
    PRIORITY_CHOICES = [
        (PRIORITY_LOW, 'Low'),
        (PRIORITY_MEDIUM, 'Medium'),
        (PRIORITY_HIGH, 'High'),
        (PRIORITY_URGENT, 'Urgent'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='support_tickets')
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.subject} ({self.get_status_display()})"


class SupportMessage(models.Model):
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_CHOICES = [
        (STATUS_SENT, 'تم الإرسال'),
        (STATUS_DELIVERED, 'تم الاستلام'),
        (STATUS_READ, 'تمت القراءة'),
    ]

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='messages')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='support_messages')
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT) # الحقل الجديد
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message by {self.user.username} on ticket {self.ticket.id}"


class AuditLog(models.Model):
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    username = models.CharField(max_length=150, blank=True, default="")
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=512)
    status_code = models.PositiveSmallIntegerField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("created_at",)),
            models.Index(fields=("user", "created_at")),
            models.Index(fields=("method", "path")),
        ]

    def __str__(self):
        actor = self.username or "anonymous"
        return f"{actor} {self.method} {self.path} -> {self.status_code}"

