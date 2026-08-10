# insurance-service/main.py
import os
from pathlib import Path
from typing import Callable
from fastapi import FastAPI
from engines.insurance import review_insurance
from provider import Provider
from case_store import init_db, make_case_router

app = FastAPI()
_provider = Provider()
init_db()


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None]) -> dict:
    return review_insurance(
        paths["policy"],
        _provider,
        models={
            "extraction": os.environ["MODEL_EXTRACTION"],  # analysis-grade model
            "vision":     os.environ["MODEL_VISION"],
        },
        emit=emit,
    )


app.include_router(make_case_router(          # Kong exposes this as /api/insurance/cases...
    service_scope="insurance",                # 403 unless the JWT carries scope "insurance"
    upload_slots={"policy": {".pdf", ".docx"}},
    min_slots_ready=["policy"],
    analyze=_analyze,
))
