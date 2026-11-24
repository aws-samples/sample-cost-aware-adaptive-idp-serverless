# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tier escalation decisions — the heart of "AI only when needed".

After a cheap tier runs, we decide whether the result is trustworthy or
whether to escalate to the next (more expensive) tier. The rule is
deliberately conservative and explainable rather than ML-based.
"""

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ClarityDecision:
    """Whether a tier's output is clear enough to accept."""

    is_clear: bool
    reason: str
    field_count: int
    template_matched: bool
    template_similarity: float = 0.0


def assess_ocr_clarity(
    document: Any,
    template_match: Optional[Any],
    min_fields: int = 3,
    min_template_similarity: float = 0.7,
) -> ClarityDecision:
    """Decide if cheap OCR (Tier 1) output is clear, or should escalate.

    Clear when we recovered enough fields AND either matched a known template
    with sufficient similarity (so spatial memory can validate positions) or
    recovered a healthy number of fields on a single-page document.
    """
    field_count = len(document.all_key_values())
    matched = template_match is not None
    similarity = getattr(template_match, "similarity_score", 0.0) if matched else 0.0

    if field_count < min_fields:
        return ClarityDecision(
            is_clear=False,
            reason=f"only {field_count} field(s) recovered (< {min_fields})",
            field_count=field_count,
            template_matched=matched,
            template_similarity=similarity,
        )
    if matched and similarity >= min_template_similarity:
        return ClarityDecision(
            is_clear=True,
            reason=f"matched template (similarity {similarity:.2f})",
            field_count=field_count,
            template_matched=True,
            template_similarity=similarity,
        )
    return ClarityDecision(
        is_clear=False,
        reason="no confident template match; escalating for structured extraction",
        field_count=field_count,
        template_matched=matched,
        template_similarity=similarity,
    )
