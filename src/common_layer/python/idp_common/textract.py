# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Textract parsing + tier escalation (Tiers 1 and 2, paid).

Two response parsers turn Textract output into the canonical
:class:`~idp_common.models.Document`:

* :func:`parse_detect_text` — Tier 1, cheap ``DetectDocumentText``. Lines are
  recovered into ``Label: value`` key/values (same heuristic as born-digital).
* :func:`parse_analyze_forms` — Tier 2, structured ``AnalyzeDocument`` with the
  FORMS feature, giving true key/value pairs with bounding boxes.

The parsing here is pure (no AWS calls), so it is unit-tested with recorded
Textract JSON. The handler wraps these with the actual Textract client.
"""

import logging
from typing import Any, Dict, List

from idp_common.models import BBox, Document, KeyValue, Page, Word
from idp_common.text_lines import LineSpan, page_lines_to_key_values

logger = logging.getLogger(__name__)


def _bbox(geometry: Dict[str, Any]) -> BBox:
    box = geometry["BoundingBox"]
    return BBox(
        x=max(0.0, min(1.0, float(box["Left"]))),
        y=max(0.0, min(1.0, float(box["Top"]))),
        width=max(1e-4, min(1.0, float(box["Width"]))),
        height=max(1e-4, min(1.0, float(box["Height"]))),
    )


def _blocks_by_id(blocks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {b["Id"]: b for b in blocks if "Id" in b}


def parse_detect_text(response: Dict[str, Any]) -> Document:
    """Parse ``DetectDocumentText`` blocks into a Document (Tier 1).

    LINE blocks are grouped per page and run through the ``Label: value``
    heuristic. This is the cheap OCR tier.
    """
    lines_by_page: Dict[int, List[LineSpan]] = {}
    max_page = 1
    for block in response.get("Blocks", []):
        if block.get("BlockType") != "LINE":
            continue
        page = int(block.get("Page", 1))
        max_page = max(max_page, page)
        lines_by_page.setdefault(page, []).append(
            LineSpan(text=block.get("Text", ""), bbox=_bbox(block["Geometry"]))
        )

    pages: List[Page] = []
    for page_number in range(1, max_page + 1):
        lines = lines_by_page.get(page_number, [])
        pages.append(
            Page(
                key_values=page_lines_to_key_values(lines, page_number),
                has_text_layer=bool(lines),
            )
        )
    return Document(pages=pages)


def _collect_child_text(
    block: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]
) -> str:
    words: List[str] = []
    for rel in block.get("Relationships", []) or []:
        if rel.get("Type") != "CHILD":
            continue
        for child_id in rel.get("Ids", []):
            child = by_id.get(child_id, {})
            if child.get("BlockType") in ("WORD", "SELECTION_ELEMENT"):
                if child.get("BlockType") == "WORD":
                    words.append(child.get("Text", ""))
                elif child.get("SelectionStatus") == "SELECTED":
                    words.append("[X]")
    return " ".join(w for w in words if w).strip()


def parse_analyze_forms(response: Dict[str, Any]) -> Document:
    """Parse ``AnalyzeDocument`` (FORMS) into a Document of true key/values."""
    blocks = response.get("Blocks", [])
    by_id = _blocks_by_id(blocks)

    pages: Dict[int, List[KeyValue]] = {}
    max_page = 1
    for block in blocks:
        if block.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in (block.get("EntityTypes") or []):
            continue
        page = int(block.get("Page", 1))
        max_page = max(max_page, page)

        key_text = _collect_child_text(block, by_id)
        if not key_text:
            continue

        value_text = ""
        for rel in block.get("Relationships", []) or []:
            if rel.get("Type") != "VALUE":
                continue
            for value_id in rel.get("Ids", []):
                value_block = by_id.get(value_id, {})
                value_text = _collect_child_text(value_block, by_id)

        confidence = float(block.get("Confidence", 100.0)) / 100.0
        pages.setdefault(page, []).append(
            KeyValue(
                key=[Word(tok) for tok in key_text.split()],
                bbox=_bbox(block["Geometry"]),
                page=page,
                confidence=confidence,
                value=value_text,
            )
        )

    return Document(
        pages=[
            Page(key_values=pages.get(p, []), has_text_layer=True)
            for p in range(1, max_page + 1)
        ]
    )


def count_tables(response: Dict[str, Any]) -> int:
    """Number of TABLE regions detected (drives Phase 3 table handling)."""
    return sum(1 for b in response.get("Blocks", []) if b.get("BlockType") == "TABLE")
