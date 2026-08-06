"""Profile configuration: inheritance from the shipped defaults.

A profile stores only *overrides* (ProfileConfigOverride rows); every lookup
falls through to the factory default — the prompt files shipped with the
engines and the model/parameter settings from the environment. Deleting an
override IS "reset to default", and un-overridden values automatically follow
future product updates.

The registry below also drives the per-module "process overview" screen:
each module lists its pipeline stages and which settings each stage uses, so
the person responsible can see what a module does and tune it per profile.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.auth.deps import (
    current_user,
    get_profile_or_404,
    require_config_editor,
    require_profile_member,
)
from app.core.config import settings
from app.core.db import db_session
from app.models import ProfileConfigOverride, User, utcnow

router = APIRouter(prefix="/api", tags=["config"])

_ENGINES = Path(__file__).resolve().parents[1] / "engines"


def _file(rel: str) -> Callable[[], str]:
    path = _ENGINES / rel
    return lambda: path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ConfigKey:
    key: str
    label: str
    kind: str  # prompt | number | model
    default: Callable[[], str]
    description: str = ""


CONFIG_KEYS: dict[str, ConfigKey] = {
    k.key: k
    for k in [
        # -- shared extraction ------------------------------------------------
        ConfigKey(
            "extraction.transcription.prompt", "Vision transcription prompt", "prompt",
            _file("prompts/extraction_transcription.md"),
            "Instructions for page-by-page transcription of scanned documents.",
        ),
        # -- document generation ----------------------------------------------
        ConfigKey(
            "docgen.meta_analyzer.prompt", "Template descriptor prompt", "prompt",
            _file("docgen/prompts/meta_analyzer.md"),
            "Produces the markdown descriptor for an uploaded template. The "
            "## Overview / ## Selection headings are load-bearing: the "
            "selection stage condenses descriptors by those headings.",
        ),
        ConfigKey(
            "docgen.credit_analysis.prompt", "Credit analysis prompt", "prompt",
            _file("docgen/prompts/credit_analysis.md"),
            "Optional pre-documentation review of the extracted case.",
        ),
        ConfigKey(
            "docgen.selector.prompt", "Document selection prompt", "prompt",
            _file("docgen/prompts/selector.md"),
            "Chooses which templates a case requires. WARNING: the JSON "
            "output contract described inside is parsed by the system — "
            "changing it can break selection.",
        ),
        ConfigKey(
            "docgen.fill_agent.prompt", "Document fill prompt", "prompt",
            _file("docgen/prompts/fill_agent.md"),
            "Produces the fill operations applied to the template. WARNING: "
            "the operations JSON contract is parsed by the system.",
        ),
        # -- collateral ---------------------------------------------------------
        ConfigKey(
            "collateral.extraction.prompt", "Field extraction prompt", "prompt",
            _file("prompts/collateral_extraction.md"),
            "Extracts the CAD field set from legal opinion / title document.",
        ),
        ConfigKey(
            "collateral.observations.prompt", "Observations prompt", "prompt",
            _file("prompts/collateral_observations.md"),
            "Writes plain-English observations per discrepancy.",
        ),
        # -- valuation -----------------------------------------------------------
        ConfigKey(
            "valuation.extraction.prompt", "Report extraction prompt", "prompt",
            _file("prompts/valuation_extraction.md"),
            "Extracts the 13-field set from a valuation report.",
        ),
        ConfigKey(
            "valuation.cushion_pct", "Cushion (%)", "number", lambda: "30.0",
            "Margin held back from the property value when computing the net "
            "drawable amount.",
        ),
        ConfigKey(
            "valuation.expiry_years", "Report validity (years)", "number", lambda: "3",
            "A report older than this raises an expiry alert.",
        ),
        # -- insurance ------------------------------------------------------------
        ConfigKey(
            "insurance.extraction.prompt", "Policy extraction prompt", "prompt",
            _file("insurance_extraction.md"),
            "Structures the insurance policy into the extraction schema.",
        ),
        ConfigKey(
            "insurance.analysis.prompt", "Compliance analysis prompt", "prompt",
            _file("insurance_analysis.md"),
            "Assesses the structured policy against the bank's rules.",
        ),
        # -- policy QA -----------------------------------------------------------
        ConfigKey(
            "policy_qa.system.prompt", "Answer grounding prompt", "prompt",
            _file("prompts/policy_qa_system.md"),
            "Grounding rules for policy answers, including the exact refusal "
            "wording used when the policy has no answer.",
        ),
        # -- model roles ----------------------------------------------------------
        ConfigKey("model.extraction", "Extraction model", "model",
                  lambda: settings.llm_model_extraction),
        ConfigKey("model.vision", "Vision/OCR model", "model",
                  lambda: settings.llm_model_vision),
        ConfigKey("model.selection", "Selection model", "model",
                  lambda: settings.llm_model_selection),
        ConfigKey("model.fill", "Fill model", "model",
                  lambda: settings.llm_model_fill),
        ConfigKey("model.analysis", "Analysis model", "model",
                  lambda: settings.llm_model_analysis),
        ConfigKey("model.chat", "Chat model", "model",
                  lambda: settings.llm_model_chat),
        ConfigKey("model.embedding", "Embedding model", "model",
                  lambda: settings.llm_model_embedding),
    ]
}

# The process map: what each module does, stage by stage, and which settings
# each stage uses. Drives GET /api/profiles/{pid}/config.
MODULE_PROCESS: list[dict] = [
    {
        "module": "docgen",
        "title": "Document Generation",
        "stages": [
            {"stage": "extract", "title": "Extract",
             "description": "Read the uploaded credit application. Text documents are "
                            "extracted directly; scanned PDFs are transcribed page by "
                            "page with the vision model.",
             "keys": ["extraction.transcription.prompt", "model.vision"]},
            {"stage": "analyze", "title": "Analyze (optional)",
             "description": "Pre-documentation credit review of the extracted case.",
             "keys": ["docgen.credit_analysis.prompt", "model.analysis"]},
            {"stage": "select", "title": "Select",
             "description": "Decide which templates this case requires, and how many "
                            "instances of each.",
             "keys": ["docgen.selector.prompt", "model.selection"]},
            {"stage": "fill", "title": "Generate",
             "description": "Fill each selected template with case data; every change "
                            "is recorded in the provenance audit file.",
             "keys": ["docgen.fill_agent.prompt", "model.fill"]},
            {"stage": "descriptor", "title": "Template descriptors",
             "description": "When a template version is analyzed, this prompt builds "
                            "its descriptor (used by Select and Generate).",
             "keys": ["docgen.meta_analyzer.prompt", "model.analysis"]},
        ],
    },
    {
        "module": "document",
        "title": "Document Reviewer",
        "stages": [
            {"stage": "compare", "title": "Compare",
             "description": "Deterministic word-level comparison of the original vs "
                            "the returned/signed copy. No AI involved — nothing to "
                            "configure.",
             "keys": []},
        ],
    },
    {
        "module": "collateral",
        "title": "Collateral Reviewer",
        "stages": [
            {"stage": "extract", "title": "Extract fields",
             "description": "Pull the CAD field set from both documents (vision model "
                            "for scanned PDFs).",
             "keys": ["collateral.extraction.prompt", "model.extraction", "model.vision"]},
            {"stage": "observe", "title": "Observations",
             "description": "Field-by-field comparison, then plain-English observations "
                            "per discrepancy.",
             "keys": ["collateral.observations.prompt"]},
        ],
    },
    {
        "module": "valuation",
        "title": "Valuation Reviewer",
        "stages": [
            {"stage": "extract", "title": "Extract report",
             "description": "Extract the valuation field set from the report.",
             "keys": ["valuation.extraction.prompt", "model.extraction", "model.vision"]},
            {"stage": "rules", "title": "Policy rules",
             "description": "Panel check, expiry alert, self-valuation flag, cushion "
                            "and net drawable computation.",
             "keys": ["valuation.cushion_pct", "valuation.expiry_years"]},
        ],
    },
    {
        "module": "insurance",
        "title": "Insurance Reviewer",
        "stages": [
            {"stage": "extract", "title": "Extract policy",
             "description": "Structure the insurance policy document.",
             "keys": ["insurance.extraction.prompt", "model.vision"]},
            {"stage": "analyze", "title": "Compliance analysis",
             "description": "Assess the policy against the bank's coverage and "
                            "collateral rules.",
             "keys": ["insurance.analysis.prompt", "model.analysis"]},
        ],
    },
    {
        "module": "policy_qa",
        "title": "Policy Q&A",
        "stages": [
            {"stage": "retrieve", "title": "Retrieve",
             "description": "Find the policy sections relevant to the question.",
             "keys": ["model.embedding"]},
            {"stage": "answer", "title": "Answer",
             "description": "Answer strictly from the retrieved sections, with "
                            "citations.",
             "keys": ["policy_qa.system.prompt", "model.chat"]},
        ],
    },
]


# ------------------------------------------------------------------ resolution


def _override(db: Session, profile_id: str, key: str) -> ProfileConfigOverride | None:
    return db.execute(
        select(ProfileConfigOverride).where(
            ProfileConfigOverride.profile_id == profile_id,
            ProfileConfigOverride.key == key,
        )
    ).scalar_one_or_none()


def effective(db: Session, profile_id: str, key: str) -> str:
    """The value in force for a profile: its override, else the default."""
    spec = CONFIG_KEYS[key]
    row = _override(db, profile_id, key)
    return row.value if row is not None else spec.default()


def effective_float(db: Session, profile_id: str, key: str) -> float:
    return float(effective(db, profile_id, key))


def effective_int(db: Session, profile_id: str, key: str) -> int:
    return int(float(effective(db, profile_id, key)))


def effective_model(db: Session, profile_id: str, role: str) -> str:
    return effective(db, profile_id, f"model.{role}")


def prompt_override(db: Session, profile_id: str, key: str) -> str | None:
    """The overridden prompt text, or None when the shipped default applies
    (engines then load their own frozen prompt file)."""
    row = _override(db, profile_id, key)
    return row.value if row is not None else None


# ----------------------------------------------------------------------- routes


@router.get("/profiles/{profile_id}/config")
def config_overview(
    profile_id: str,
    role: str = Depends(require_profile_member),
    db: Session = Depends(db_session),
) -> dict:
    overrides = {
        row.key: row
        for row in db.execute(
            select(ProfileConfigOverride).where(
                ProfileConfigOverride.profile_id == profile_id
            )
        ).scalars()
    }

    def key_payload(key: str) -> dict:
        spec = CONFIG_KEYS[key]
        row = overrides.get(key)
        return {
            "key": spec.key,
            "label": spec.label,
            "kind": spec.kind,
            "description": spec.description,
            "default": spec.default(),
            "value": row.value if row else spec.default(),
            "is_overridden": row is not None,
            "updated_at": row.updated_at.isoformat() if row else None,
        }

    return {
        "modules": [
            {
                "module": m["module"],
                "title": m["title"],
                "stages": [
                    {
                        "stage": s["stage"],
                        "title": s["title"],
                        "description": s["description"],
                        "keys": [key_payload(k) for k in s["keys"]],
                    }
                    for s in m["stages"]
                ],
            }
            for m in MODULE_PROCESS
        ],
        "override_count": len(overrides),
    }


class ConfigValue(BaseModel):
    value: str


def _writable_profile(db: Session, profile_id: str):
    profile = get_profile_or_404(db, profile_id)
    if profile.is_default:
        raise HTTPException(
            status_code=403,
            detail="The Default profile is the factory baseline and cannot be edited",
        )
    return profile


@router.put("/profiles/{profile_id}/config/{key:path}")
def set_config(
    profile_id: str,
    key: str,
    body: ConfigValue,
    request: Request,
    user: User = Depends(require_config_editor),
    db: Session = Depends(db_session),
) -> dict:
    if key not in CONFIG_KEYS:
        raise HTTPException(status_code=404, detail="Unknown configuration key")
    _writable_profile(db, profile_id)
    spec = CONFIG_KEYS[key]
    value = body.value
    if spec.kind == "number":
        try:
            float(value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{spec.label} must be a number")
    elif not value.strip():
        raise HTTPException(status_code=422, detail=f"{spec.label} cannot be empty")

    row = _override(db, profile_id, key)
    if row is None:
        row = ProfileConfigOverride(profile_id=profile_id, key=key, value=value)
        db.add(row)
    else:
        row.value = value
    row.updated_by = user.id
    row.updated_at = utcnow()
    audit.record(db, user, "config.override_set", profile_id=profile_id,
                 subject_type="config", subject_id=key,
                 detail={"chars": len(value)}, request=request)
    db.commit()
    return {"key": key, "value": value, "is_overridden": True}


@router.delete("/profiles/{profile_id}/config/{key:path}")
def reset_config(
    profile_id: str,
    key: str,
    request: Request,
    user: User = Depends(require_config_editor),
    db: Session = Depends(db_session),
) -> dict:
    if key not in CONFIG_KEYS:
        raise HTTPException(status_code=404, detail="Unknown configuration key")
    _writable_profile(db, profile_id)
    row = _override(db, profile_id, key)
    if row is not None:
        db.delete(row)
        audit.record(db, user, "config.override_reset", profile_id=profile_id,
                     subject_type="config", subject_id=key, request=request)
    db.commit()
    return {"key": key, "value": CONFIG_KEYS[key].default(), "is_overridden": False}
