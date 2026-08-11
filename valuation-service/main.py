# valuation-service/main.py
import os
from pathlib import Path
from typing import Callable
from fastapi import FastAPI
from engines.valuation import review_valuation
from provider import Provider
from case_store import init_db, make_case_router

app = FastAPI()
_provider = Provider()
init_db()


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None], user_sub: str) -> dict:
    # user_sub is unused: this review depends only on the uploaded report.
    return review_valuation(
        paths["report"],
        None,                        # panel_path=None → bundled default_panel.xlsx
        _provider,
        models={
            "extraction": os.environ["MODEL_EXTRACTION"],
            "vision":     os.environ["MODEL_VISION"],
        },
        emit=emit,
    )


app.include_router(make_case_router(          # Kong exposes this as /api/valuation/cases...
    service_scope="valuation",                # 403 unless the JWT carries scope "valuation"
    upload_slots={"report": {".pdf", ".docx"}},
    min_slots_ready=["report"],
    analyze=_analyze,
))
