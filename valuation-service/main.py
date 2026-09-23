# valuation-service/main.py
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from fastapi import FastAPI
from engines.valuation import review_valuation
import config_client
from provider import Provider
from case_store import init_db, make_case_router, start_outbox_relay

_provider = Provider()
init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_outbox_relay()
    yield


app = FastAPI(lifespan=lifespan)


# Fallback for when config-service can't answer — the values this service used
# to read directly. See config_client.models_for.
def _default_models() -> dict:
    return {
        "extraction": os.environ["MODEL_EXTRACTION"],
        "vision":     os.environ["MODEL_VISION"],
    }


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None],
             user_sub: str, token: str | None) -> dict:
    # user_sub is unused: beyond the caller's own model choices, this review
    # depends only on the uploaded report.
    models = config_client.models_for("valuation", token, _default_models())
    result = review_valuation(
        paths["report"],
        None,                        # panel_path=None → bundled default_panel.xlsx
        _provider,
        models=models,
        emit=emit,
    )
    # Which models produced this, so the stored result stays traceable when a
    # user later changes their choice.
    result["models"] = models
    return result


app.include_router(make_case_router(          # Kong exposes this as /api/valuation/cases...
    service_scope="valuation",                # 403 unless the JWT carries scope "valuation"
    upload_slots={"report": {".pdf", ".docx"}},
    min_slots_ready=["report"],
    analyze=_analyze,
))
