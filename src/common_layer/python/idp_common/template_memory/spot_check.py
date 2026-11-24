# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spot-checks of trusted templates against the AI tier (threat T2).

A share of documents that a trusted template would resolve on the free path is
sent to the AI tier instead. The AI result is compared with what the template
path would have recorded (the OCR key/values); a disagreement demotes the
template back to provisional so it stops resolving documents on its own.

Pure functions only (no I/O). Sampling uses :mod:`secrets`, not :mod:`random`.
"""

import secrets
from typing import Any, Dict, Optional, Tuple

from idp_common.grounding import normalize_label, normalize_value


def select_for_spot_check(percent: float) -> bool:
    """True for roughly ``percent`` % of calls (0 or less never, 100 or more always)."""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return secrets.randbelow(10000) < round(percent * 100)


def compare(
    ocr_document: Any,
    template_doc_type: Optional[str],
    ai_doc_type: Optional[str],
    ai_fields: Dict[str, Any],
) -> Tuple[bool, str]:
    """Compare an AI result with the template path's view of the same document.

    Disagrees when both document types are known and differ, or when a field
    label appears in both the OCR key/values and ``ai_fields`` with different
    normalized values. Returns ``(agree, reason)``.
    """
    if template_doc_type and ai_doc_type and template_doc_type != ai_doc_type:
        return False, f"doc_type {template_doc_type!r} != AI doc_type {ai_doc_type!r}"

    ocr_values: Dict[str, set] = {}
    for kv in ocr_document.all_key_values():
        label = normalize_label(kv.key_text)
        if label:
            ocr_values.setdefault(label, set()).add(normalize_value(kv.value))

    for name, value in (ai_fields or {}).items():
        label = normalize_label(name)
        if label not in ocr_values:
            continue
        if normalize_value(value) not in ocr_values[label]:
            return False, f"field {label!r} differs from the OCR value"
    return True, "AI result agrees with the template path"
