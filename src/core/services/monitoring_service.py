from dataclasses import asdict

from core.models import GeoData

from . import VisitOutbreakContext
from .alert_policies import get_policy_for_disease
from .cluster_service import create_cluster_from_analysis
from .outbreak_engine import evaluate_visit_outbreak
from .report_service import create_report_from_analysis


def _build_context(*, geodata: GeoData) -> VisitOutbreakContext:
    visit = geodata.visit
    return VisitOutbreakContext(
        visit_id=visit.id,
        patient_id=visit.patient_id,
        doctor_id=visit.doctor_id,
        disease_id=visit.disease_id,
        disease_code=visit.disease.disease_code,
        diagnosis_date=visit.diagnosis_date,
        latitude=geodata.latitude,
        longitude=geodata.longitude,
        region_type=geodata.region_type,
    )


def run_monitoring_for_geodata(
    *,
    geodata: GeoData,
    persist_report: bool = True,
    persist_cluster: bool = True,
) -> dict[str, object]:
    visit = geodata.visit
    policy = get_policy_for_disease(
        disease_code=visit.disease.disease_code,
        risk_level=visit.disease.risk_level,
        infection_score=visit.disease.infection_score,
        high_priority=visit.disease.high_priority,
        rare_disease=visit.disease.rare_disease,
        policy_profile=visit.disease.policy_profile,
    )
    context = _build_context(geodata=geodata)
    analysis = evaluate_visit_outbreak(context=context, policy=policy)

    persistence = {
        "report_id": None,
        "cluster_id": None,
        "report_saved": False,
        "cluster_saved": False,
        "messages": [],
    }
    if persist_report:
        try:
            persistence["report_id"] = create_report_from_analysis(
                context=context,
                analysis=analysis,
            )
            persistence["report_saved"] = True
        except ValueError as exc:
            persistence["messages"].append(str(exc))

    if persist_cluster:
        try:
            persistence["cluster_id"] = create_cluster_from_analysis(
                context=context,
                analysis=analysis,
            )
            persistence["cluster_saved"] = True
        except ValueError as exc:
            persistence["messages"].append(str(exc))

    return {
        "context": asdict(context),
        "policy": asdict(policy),
        "analysis": asdict(analysis),
        "persistence": persistence,
    }
