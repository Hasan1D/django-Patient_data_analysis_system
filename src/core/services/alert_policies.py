from dataclasses import replace

from .contracts import DiseaseAlertPolicy


CANONICAL_POLICY_CODE_ALIASES: dict[str, str] = {
    "MEA": "MEASLES",
    "MEASLES": "MEASLES",
    "1F03": "MEASLES",
    "COL": "CHOLERA",
    "CHOLERA": "CHOLERA",
    "1A00": "CHOLERA",
    "POL": "POLIO",
    "POLIO": "POLIO",
    "1C81": "POLIO",
    "SMPX": "SMALLPOX",
    "SMALLPOX": "SMALLPOX",
    "1E70": "SMALLPOX",
    "HEPA": "HEPA",
    "1E50.0": "HEPA",
    "PLG": "PLAGUE",
    "PLAGUE": "PLAGUE",
    "1B93": "PLAGUE",
    "EBOLA": "EBOLA",
    "1D60.01": "EBOLA",
    "SARS": "SARS",
    "1D65": "SARS",
    "MERS": "MERS",
    "1D64": "MERS",
    "MPOX": "MPOX",
    "1E71": "MPOX",
    "INFLUENZA": "INFLUENZA",
    "1E32": "INFLUENZA",
    "PERTUSSIS": "PERTUSSIS",
    "1C12": "PERTUSSIS",
    "TYPHOID": "TYPHOID",
    "1A07": "TYPHOID",
}

HIGH_PRIORITY_DISEASE_CODES: tuple[str, ...] = (
    "MEASLES",
    "COVID",
    "HFV",
    "EBOLA",
    "SARS",
    "MERS",
    "MPOX",
)
RARE_DISEASE_CODES: tuple[str, ...] = (
    "POLIO",
    "PLAGUE",
    "SMALLPOX",
    "EBOLA",
)

PROFILE_OVERRIDES: dict[str, dict[str, object]] = {
    "high_priority": {
        "high_priority": True,
        "lookback_days": 10,
        "baseline_window_days": 21,
        "radius_km": 3.0,
        "severity_weight": 1.9,
        "density_weight": 1.35,
        "trend_weight": 1.25,
        "cluster_case_threshold": 2,
        "critical_case_threshold": 4,
    },
    "rare": {
        "rare_disease": True,
        "high_priority": True,
        "lookback_days": 30,
        "baseline_window_days": 30,
        "radius_km": 10.0,
        "severity_weight": 2.3,
        "density_weight": 1.5,
        "trend_weight": 1.2,
        "rarity_weight": 3.0,
        "cluster_case_threshold": 1,
        "critical_case_threshold": 1,
    },
    "cluster_sensitive": {
        "lookback_days": 7,
        "baseline_window_days": 14,
        "radius_km": 2.0,
        "density_weight": 1.7,
        "trend_weight": 1.5,
        "cluster_case_threshold": 2,
        "critical_case_threshold": 4,
    },
    "surge_sensitive": {
        "lookback_days": 7,
        "baseline_window_days": 21,
        "radius_km": 3.0,
        "density_weight": 1.2,
        "trend_weight": 1.7,
        "cluster_case_threshold": 3,
        "critical_case_threshold": 5,
    },
    "environmental_signal": {
        "lookback_days": 30,
        "baseline_window_days": 60,
        "radius_km": 5.0,
        "severity_weight": 0.75,
        "density_weight": 0.9,
        "trend_weight": 1.25,
        "cluster_case_threshold": 5,
        "critical_case_threshold": 9,
    },
}

POLICY_OVERRIDES: dict[str, dict[str, object]] = {
    "MEASLES": {
        "high_priority": True,
        "lookback_days": 14,
        "baseline_window_days": 21,
        "radius_km": 3.0,
        "severity_weight": 2.2,
        "density_weight": 1.4,
        "trend_weight": 1.3,
        "cluster_case_threshold": 1,
        "critical_case_threshold": 2,
    },
    "CHOLERA": {
        "lookback_days": 7,
        "baseline_window_days": 14,
        "radius_km": 3.0,
        "density_weight": 1.8,
        "trend_weight": 1.8,
        "cluster_case_threshold": 2,
        "critical_case_threshold": 4,
    },
    "POLIO": {
        "rare_disease": True,
        "high_priority": True,
        "lookback_days": 30,
        "baseline_window_days": 30,
        "radius_km": 10.0,
        "severity_weight": 2.5,
        "density_weight": 1.5,
        "trend_weight": 1.2,
        "rarity_weight": 3.0,
        "cluster_case_threshold": 1,
        "critical_case_threshold": 1,
    },
    "HEPA": {
        "lookback_days": 5,
        "baseline_window_days": 14,
        "radius_km": 2.0,
        "density_weight": 1.6,
        "trend_weight": 1.7,
        "cluster_case_threshold": 3,
        "critical_case_threshold": 5,
    },
    "FPO": {
        "lookback_days": 2,
        "baseline_window_days": 7,
        "radius_km": 1.0,
        "density_weight": 1.9,
        "trend_weight": 1.7,
        "cluster_case_threshold": 4,
        "critical_case_threshold": 8,
    },
}


def _normalize_disease_code(disease_code: str) -> str:
    normalized_code = disease_code.strip().upper()
    if not normalized_code:
        raise ValueError("disease_code cannot be blank.")
    return normalized_code


def _canonical_policy_code(disease_code: str) -> str:
    return CANONICAL_POLICY_CODE_ALIASES.get(disease_code, disease_code)


def _normalize_infection_signal(infection_score: float) -> float:
    normalized_score = max(infection_score, 0.0)
    if normalized_score <= 1.0:
        return normalized_score
    if normalized_score <= 5.0:
        return round(normalized_score / 5.0, 3)
    return 1.0


def _normalize_policy_profile(policy_profile: str | None) -> str | None:
    if policy_profile is None:
        return None
    normalized_profile = policy_profile.strip().lower()
    if not normalized_profile:
        return None
    return normalized_profile


def get_policy_for_disease(
    *,
    disease_code: str,
    risk_level: int,
    infection_score: float,
    high_priority: bool = False,
    rare_disease: bool = False,
    policy_profile: str | None = None,
) -> DiseaseAlertPolicy:
    """
    Return the alert policy for a disease.

    The returned policy mixes:
    - a generic baseline derived from disease severity metadata
    - explicit overrides for diseases that need special operational behavior
    """
    normalized_code = _normalize_disease_code(disease_code)
    canonical_code = _canonical_policy_code(normalized_code)
    normalized_profile = _normalize_policy_profile(policy_profile)
    infection_signal = _normalize_infection_signal(infection_score)

    derived_high_priority = (
        high_priority
        or canonical_code in HIGH_PRIORITY_DISEASE_CODES
        or normalized_profile in {"high_priority", "rare"}
        or (risk_level >= 5 and infection_signal >= 0.85)
    )
    derived_rare_disease = (
        rare_disease
        or canonical_code in RARE_DISEASE_CODES
        or normalized_profile == "rare"
    )

    cluster_case_threshold = 2 if derived_high_priority else 3
    critical_case_threshold = 4 if derived_high_priority else 6

    base_policy = DiseaseAlertPolicy(
        disease_code=normalized_code,
        lookback_days=7,
        baseline_window_days=7,
        radius_km=3.0,
        rare_disease=derived_rare_disease,
        high_priority=derived_high_priority,
        severity_weight=round(1.0 + max(risk_level, 0) * 0.15 + infection_signal * 0.35, 2),
        density_weight=round(1.0 + max(infection_signal - 0.4, 0.0) * 0.9, 2),
        trend_weight=round(1.0 + max(risk_level - 2, 0) * 0.10 + (0.15 if infection_signal >= 0.75 else 0.0), 2),
        rarity_weight=3.0 if derived_rare_disease else 1.0,
        cluster_case_threshold=1 if derived_rare_disease else cluster_case_threshold,
        critical_case_threshold=1 if derived_rare_disease else critical_case_threshold,
    )

    profile_overrides = PROFILE_OVERRIDES.get(normalized_profile or "")
    if profile_overrides:
        base_policy = replace(base_policy, **profile_overrides)

    if derived_rare_disease and not base_policy.rare_disease:
        base_policy = replace(
            base_policy,
            rare_disease=True,
            high_priority=True,
            rarity_weight=max(base_policy.rarity_weight, 3.0),
            cluster_case_threshold=1,
            critical_case_threshold=1,
        )
    elif derived_high_priority and not base_policy.high_priority:
        base_policy = replace(base_policy, high_priority=True)

    overrides = POLICY_OVERRIDES.get(canonical_code)
    if overrides:
        base_policy = replace(base_policy, **overrides)

    return base_policy
