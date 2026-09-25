"""Did the extraction read the kind of document the endpoint expects?

Post a valuation report to the insurance endpoint, or an insurance policy to
the valuation endpoint, and the extraction runs happily: every business
document has a name, an address, an amount and a date, so the payload fills and
the answer looks finished. That is a false pass, and a silent one.

The check is the same shape for both reviewers, so the mechanics live here and
each reviewer supplies its own field list:

* count the fields that identify the document *as that kind*
* below a threshold, reject rather than reporting nulls

**Choosing the fields is the whole job, and it is empirical.** Insurance's five
were picked by diffing two real runs - a policy scored 5, a valuation report 0.
Two rules came out of that and generalise:

1. **Exclude what fills for anything.** Amount, currency, a date, a party name
   and an address are present on every document and carry no signal. They are
   also what the friendly-looking payload fields are built from, which is why a
   wrong document produces a payload that looks almost complete.
2. **Exclude what is specific to one variant.** ``voyage_from`` separates a
   transit policy from a valuation report perfectly, and would still be wrong
   to count, because a Fire policy leaves it empty too. A field qualifies only
   if it is present on that kind of document in ANY of its variants.

A third rule earned separately: **exclude anything the prompt defaults.** A
field the extraction prompt fills in when the document is silent is always
filled, whatever was uploaded.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

#: A field group: a human label, and the paths that can satisfy it. More than
#: one path means any of them scores the group once - two labels for the same
#: fact must not be worth two points.
FieldGroup = tuple[str, tuple[str, ...]]

#: What a model writes when it means "nothing here".
EMPTY_STRINGS = frozenset({
    "", "-", "--", "n/a", "na", "none", "null", "nil", "not stated",
    "not available", "not mentioned", "not applicable", "not provided",
    "unknown", "tbd",
})


def is_filled(value: Any) -> bool:
    """Is this a real value?

    The extractions are model-authored, so "missing" arrives in several shapes:
    ``None``, an empty string or collection, and the words a model reaches for
    when a field is absent. ``0`` and ``False`` are real values.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in EMPTY_STRINGS
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def resolve(document: Mapping[str, Any], dotted: str) -> Any:
    """Follow a dotted path, returning None rather than raising on a gap.

    Handles both shapes in use: insurance nests its extraction under sections
    (``basic_info.insurer_name``), valuation keeps it flat
    (``valuation_company``).
    """
    current: Any = document
    for segment in dotted.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def score(
    document: Mapping[str, Any] | None,
    groups: Iterable[FieldGroup],
) -> tuple[list[str], list[str]]:
    """``(found, missing)`` group labels, in the order the groups were declared."""
    document = document or {}
    found: list[str] = []
    missing: list[str] = []
    for label, paths in groups:
        if any(is_filled(resolve(document, path)) for path in paths):
            found.append(label)
        else:
            missing.append(label)
    return found, missing


def shortfall_detail(
    found: list[str],
    missing: list[str],
    total: int,
    *,
    what: str,
    advice: str,
) -> str:
    """The rejection message: what was read, what was not, and what to do.

    Names the missing fields rather than accusing the caller of uploading the
    wrong file - a genuine document that scanned badly produces the same
    emptiness, and both causes have the same remedy.
    """
    return (
        f"Could not extract enough {what} to review this document. "
        f"Found {len(found)} of {total} identifying fields"
        f"{' (' + ', '.join(found) + ')' if found else ''}; "
        f"missing {', '.join(missing)}. {advice}"
    )
