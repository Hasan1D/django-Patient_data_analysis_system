REFERENCE_SOURCE_NAME = "icd11_cdc_nci_seed"

RARE_REFERENCE_CODES: set[str] = {
    "1E70",      # Smallpox
    "1C81",      # Acute poliomyelitis
    "1B93",      # Plague
    "1D60.01",   # Ebola virus disease
}

HIGH_PRIORITY_REFERENCE_CODES: set[str] = {
    "1A00",      # Cholera
    "1F03",      # Measles
    "1E71",      # Mpox
    "1D65",      # SARS
    "1D64",      # MERS
    "1D60.01",   # Ebola
    "1C1C.0",    # Meningococcal meningitis
    "1A07",      # Typhoid fever
    "1E50.0",    # Viral hepatitis A
}

WATER_OR_FOOD_VECTOR_MARKERS: tuple[str, ...] = (
    "waterborne",
    "foodborne",
    "fecal_oral",
)
RESPIRATORY_VECTOR_MARKERS: tuple[str, ...] = (
    "airborne",
    "droplet",
    "respiratory",
    "close_contact",
)


def _normalize_text(value: str) -> str:
    return value.strip()


def classify_reference_disease(
    *,
    disease_code: str,
    name: str,
    disease_type: str,
    transmission_vector: str,
    risk_level: int,
    infection_score: float,
) -> dict[str, object]:
    normalized_code = _normalize_text(disease_code).upper()
    normalized_name = _normalize_text(name)
    normalized_type = _normalize_text(disease_type).lower()
    normalized_vector = _normalize_text(transmission_vector).lower()
    lower_name = normalized_name.lower()

    rare_disease = normalized_code in RARE_REFERENCE_CODES or any(
        keyword in lower_name
        for keyword in ("smallpox", "poliomyelitis", "plague", "ebola")
    )
    high_priority = rare_disease or normalized_code in HIGH_PRIORITY_REFERENCE_CODES

    if rare_disease:
        policy_profile = "rare"
    elif normalized_type == "high_alert_notifiable":
        policy_profile = "high_priority"
        high_priority = True
    elif normalized_type == "cancer_environmental_signal":
        policy_profile = "environmental_signal"
    elif normalized_type == "gastrointestinal" or any(
        marker in normalized_vector for marker in WATER_OR_FOOD_VECTOR_MARKERS
    ):
        policy_profile = "cluster_sensitive"
    elif normalized_type == "respiratory" or any(
        marker in normalized_vector for marker in RESPIRATORY_VECTOR_MARKERS
    ):
        policy_profile = "surge_sensitive"
    elif normalized_type == "epidemic_infectious" and (
        infection_score >= 0.75 or risk_level >= 4
    ):
        policy_profile = "high_priority"
        high_priority = True
    elif risk_level >= 5 and infection_score >= 0.8:
        policy_profile = "high_priority"
        high_priority = True
    else:
        policy_profile = "general"

    if policy_profile in {"high_priority", "rare"}:
        high_priority = True

    return {
        "source_record_id": None,
        "source_name": REFERENCE_SOURCE_NAME,
        "is_reference": True,
        "policy_profile": policy_profile,
        "high_priority": high_priority,
        "rare_disease": rare_disease,
    }

