# collateral-service/main.py
import os
from pathlib import Path
from typing import Callable
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from engines.collateral import review_collateral
from provider import Provider
from case_store import get_db, get_owned_case, init_db, make_case_router, pair_dir
from security import require_scope

def _models() -> dict:
    return {
        "extraction": os.environ["MODEL_EXTRACTION"],
        "vision":     os.environ["MODEL_VISION"],
    }

app = FastAPI()
_provider = Provider()
init_db()

# Upload slot -> the engine's name for the document in that slot.
DOC_FOR_SLOT = {"legal": "legal_opinion", "property": "property_document"}

# The text each document was read as, kept beside that pair's uploads. In its
# own subdirectory because a `{slot}.*` name would collide with the glob
# case_store uses to find the uploaded file for a slot.
_SOURCE_SUBDIR = "extracted"


def _source_path(directory: Path, slot: str) -> Path:
    return directory / _SOURCE_SUBDIR / f"{slot}.txt"


def _analyze(paths: dict[str, Path], emit: Callable[[str, str], None], user_sub: str) -> dict:
    # user_sub is unused: this review depends only on the two uploads.
    result = review_collateral(
        paths["legal"],
        paths["property"],
        _provider,
        models=_models(),
        # prompts=None → engine uses its bundled .md files
        emit=emit,
    )

    # Each extracted value carries an evidence span — character offsets into the
    # text the model actually read — so that text has to be kept verbatim for
    # the offsets to mean anything (re-extracting later would re-run vision OCR
    # and produce different characters). It's written beside the pair's uploads
    # and POPPED off the result: the offsets are small and belong in the stored
    # result and the audit trail, two whole documents' text does not.
    texts = result.pop("source_texts", {})
    for slot, document in DOC_FOR_SLOT.items():
        text = texts.get(document)
        if text is None or slot not in paths:
            continue
        destination = _source_path(paths[slot].parent, slot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    return result


app.include_router(make_case_router(          # Kong exposes this as /api/collateral/cases...
    service_scope="collateral",               # 403 unless the JWT carries scope "collateral"
    upload_slots={"legal": {".pdf", ".docx"}, "property": {".pdf", ".docx"}},
    min_slots_ready=["legal", "property"],
    analyze=_analyze,
))


# Collateral's own case-scoped route, on top of the shared /cases router: the
# reviewer needs to see WHERE in a document each extracted value came from, and
# that means serving the text those citations point into. Kept out of
# case_store.py because only this service records citations; the ownership rule
# is reused from there rather than re-implemented.
_source_router = APIRouter(prefix="/cases", tags=["cases"])


@_source_router.get("/{case_id}/pairs/{index}/source/{slot}",
                    response_class=PlainTextResponse)
def get_source_text(
    case_id: str,
    index: int,
    slot: str,
    user: dict = Depends(require_scope("collateral")),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The exact text one document was read as, for one pair.

    This is what the review's evidence offsets index, so it is served verbatim
    from what analysis wrote — never re-extracted. 404 until that pair has been
    analyzed (or if it was analyzed before citations were recorded), which the
    caller should treat as "re-run the review", not as an error.
    """
    if slot not in DOC_FOR_SLOT:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown slot '{slot}'; expected one of: {', '.join(sorted(DOC_FOR_SLOT))}",
        )
    user_sub = user.get("sub")
    if not user_sub:
        raise HTTPException(status_code=401, detail="Token has no subject")
    get_owned_case(db, case_id, user_sub)   # 404s unless this user owns the case
    if index < 0:
        raise HTTPException(status_code=404, detail=f"No pair {index + 1} on this case")

    path = _source_path(pair_dir(case_id, index), slot)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The extracted text for this document isn't stored — review this pair again",
        )
    return PlainTextResponse(path.read_text(encoding="utf-8"))


app.include_router(_source_router)
