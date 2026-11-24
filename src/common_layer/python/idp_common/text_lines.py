# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Source-agnostic line → ``Label: value`` key/value recovery.

These helpers turn positioned lines of text (from any source — Textract OCR,
pypdf text extraction, etc.) into canonical :class:`~idp_common.models.KeyValue`
pairs using a light ``Label: value`` heuristic. Keeping this logic in a neutral
module lets the Textract parser and the PDF-split path share one implementation
without dragging in any provider-specific dependency.

The parsing is pure and easily unit-tested.
"""

import re
from dataclasses import dataclass
from typing import List, Optional

from idp_common.models import BBox, KeyValue, Word

# "Label: value" where the label is short-ish and the value non-empty.
_KV_PATTERN = re.compile(r"^\s*([A-Za-z][\w .,/&()'-]{0,48}?)\s*[:\-]\s+(\S.*)$")

# A line is considered usable text only if it has at least one alphanumeric char.
_TEXT_PATTERN = re.compile(r"[A-Za-z0-9]")


@dataclass
class LineSpan:
    """A line of text with its normalized bounding box on the page."""

    text: str
    bbox: BBox


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` into the inclusive ``[low, high]`` range."""
    return max(low, min(high, value))


def line_to_key_value(line: LineSpan, page_number: int) -> Optional[KeyValue]:
    """Parse one line into a KeyValue if it matches the ``Label: value`` shape."""
    match = _KV_PATTERN.match(line.text)
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if not label or not value:
        return None
    return KeyValue(
        key=[Word(tok) for tok in label.split()],
        bbox=line.bbox,
        page=page_number,
        confidence=1.0,  # exact text extraction
        value=value,
    )


def page_lines_to_key_values(
    lines: List[LineSpan], page_number: int
) -> List[KeyValue]:
    """Convert all matching lines on a page into KeyValues."""
    result = []
    for line in lines:
        kv = line_to_key_value(line, page_number)
        if kv is not None:
            result.append(kv)
    return result


def has_text_layer(lines: List[LineSpan]) -> bool:
    """True when at least one line carries alphanumeric text."""
    return any(_TEXT_PATTERN.search(line.text) for line in lines)
