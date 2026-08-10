# collateral-service/main.py
import os
from pathlib import Path
from typing import Callable
from fastapi import FastAPI
from engines.collateral import review_collateral
from provider import Provider
from case_store import init_db, make_case_router

def _models() -> dict:
    return {
        "extraction": os.environ["MODEL_EXTRACTION"],
        "vision":     os.environ["MODEL_VISION"],
    }

app = FastAPI()
_provider = Provider()
init_db()


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None]) -> dict:
    return review_collateral(
        paths["legal"],
        paths["property"],
        _provider,
        models=_models(),
        # prompts=None → engine uses its bundled .md files
        emit=emit,
    )


app.include_router(make_case_router(          # Kong exposes this as /api/collateral/cases...
    service_scope="collateral",               # 403 unless the JWT carries scope "collateral"
    upload_slots={"legal": {".pdf", ".docx"}, "property": {".pdf", ".docx"}},
    min_slots_ready=["legal", "property"],
    analyze=_analyze,
))
