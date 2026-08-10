"""Document-generation engine package (ported from the POC ``subsystems/``).

Four pure-logic engines sharing the frozen prompts in ``prompts/``:

- :mod:`app.engines.docgen.meta_analyzer` — template → descriptor markdown
- :mod:`app.engines.docgen.selector` — case text + descriptors → selection JSON
- :mod:`app.engines.docgen.fill_agent` — template instance → filled Document
- :mod:`app.engines.docgen.credit_analysis` — case text → analysis markdown

No FastAPI / SQLAlchemy / settings imports here: the LLM provider, model
names, file paths, domain knowledge and emit callbacks all arrive as function
arguments. Prompt wording is frozen domain IP — the ``prompts/*.md`` files are
verbatim copies of the legacy ``subsystems/*/prompt.md`` and must not be
edited casually.

Note: ``selector.build_messages`` and ``fill_agent.build_messages`` are both
part of the ported API; access them via their modules — they are deliberately
not re-exported here to avoid shadowing.
"""
from app.engines.docgen.credit_analysis import analyze_case
from app.engines.docgen.fill_agent import (
    FillResult,
    execute_operations,
    fill_document,
    serialize_template,
)
from app.engines.docgen.meta_analyzer import analyze_template
from app.engines.docgen.selector import condense_descriptors, select_documents

__all__ = [
    "FillResult",
    "analyze_case",
    "analyze_template",
    "condense_descriptors",
    "execute_operations",
    "fill_document",
    "select_documents",
    "serialize_template",
]
