# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ground LLM-extracted values in the OCR text of the same document.

A value the model returns is only trusted for compliance when the same value
appears in the OCR output under the same label. This blocks the main prompt
injection outcome: a document that tells the model to "report 999" (or a value
hidden off-page) produces a field that is not in the OCR text, so it is marked
ungrounded and any rule that uses it is sent to review instead of passing.

Pure functions only (no I/O) so they are unit-testable and reusable.
"""

import re
from typing import Any, Dict, List, Optional

from idp_common.models import Document

_LABEL_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[a-z0-9.\-]+")
# Unit exponents: "mm²"/"mm^2" are written as "mm2" so every form of a unit is
# one token ("35 N/mm2"), never a stray "2".
_SUPERSCRIPTS = str.maketrans({"\u00b2": "2", "\u00b3": "3"})
_CARET_EXPONENT_RE = re.compile(r"(?<=[A-Za-z])\^(?=[23](?![0-9]))")
# A single 2/3 directly after a unit's letters ("m2", "kN/m2", "cm3"), not
# followed by another digit, letter or decimal part.
_UNIT_EXPONENT_RE = re.compile(r"(?<=[A-Za-z])[23](?![0-9A-Za-z]|\.\d)")


def canonical_units(value: Any) -> str:
    """Write unit exponents in ASCII form: ``"N/mm²"`` and ``"N/mm^2"`` -> ``"N/mm2"``."""
    return _CARET_EXPONENT_RE.sub("", str(value).translate(_SUPERSCRIPTS))


def strip_unit_exponents(value: Any) -> str:
    """Drop ASCII unit exponents so only the measured number remains.

    ``"35 N/mm2"`` -> ``"35 N/mm"``; ``"30-35"`` and ``"C30"`` are unchanged.
    """
    return _UNIT_EXPONENT_RE.sub("", canonical_units(value))


def normalize_label(label: Any) -> str:
    """Lower-case a label and collapse non-alphanumerics to single underscores."""
    return _LABEL_RE.sub("_", str(label).lower()).strip("_")


def normalize_value(value: Any) -> str:
    """Canonical token string for a value (lower-case, commas removed)."""
    return " ".join(_tokens(value))


def _tokens(value: Any) -> List[str]:
    return _TOKEN_RE.findall(canonical_units(value).lower().replace(",", ""))


def all_verified(
    field_provenance: Dict[str, Dict[str, Any]],
    fields: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the document has kept fields and none of them is ungrounded.

    Used to decide whether an AI-processed document may count toward template
    trust (threat T2): one ungrounded value (``grounded is False``) means the
    model output was not verified. A document with no kept fields verified
    nothing, so it does not count either.

    When ``fields`` (name -> value) is given, only fields that carry a
    non-empty value are considered: an empty or absent value asserts nothing,
    so it cannot block trust. A non-empty value that is ungrounded (for BDA:
    no bounding box) still counts as unverified.
    """
    if fields is not None:
        field_provenance = {
            name: prov
            for name, prov in field_provenance.items()
            if fields.get(name) is not None and str(fields[name]).strip()
        }
    if not field_provenance:
        return False
    return all((prov or {}).get("grounded") is not False for prov in field_provenance.values())


def ocr_index(document: Document) -> Dict[str, List[str]]:
    """Map each normalized OCR label to the list of values seen under it."""
    index: Dict[str, List[str]] = {}
    for kv in document.all_key_values():
        label = normalize_label(kv.key_text)
        if not label:
            continue
        index.setdefault(label, []).append(kv.value)
    return index


def is_grounded(field_name: Any, value: Any, index: Dict[str, List[str]]) -> bool:
    """True when ``value`` appears (as a whole token sequence) under ``field_name``.

    ``"120"`` is grounded in ``"120 kN"``; ``"5"`` is not grounded in ``"50"``.
    An empty value, or a label that is not in the OCR index, is never grounded.
    """
    tokens = _tokens(value)
    if not tokens:
        return False
    needle = f" {' '.join(tokens)} "
    for candidate in index.get(normalize_label(field_name), []):
        if needle in f" {' '.join(_tokens(candidate))} ":
            return True
    return False
