# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Canonical document model shared across the whole pipeline.

This is the single representation every tier produces and consumes:

* Born-digital extraction (Tier 0) builds it from a PDF text layer.
* Textract tiers (Tier 1/2) build it from OCR output.
* The spatial memory engine reads ``document.pages[].key_values[]`` directly.

Coordinates are normalized to ``[0.0, 1.0]`` relative to the page so the model
is resolution- and DPI-independent.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class BBox:
    """Normalized bounding box: x, y, width, height in ``[0.0, 1.0]``."""

    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> Dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: Dict[str, float]) -> "BBox":
        return cls(
            x=float(data["x"]),
            y=float(data["y"]),
            width=float(data["width"]),
            height=float(data["height"]),
        )


@dataclass
class Word:
    """A single token of text within a key or value."""

    text: str


@dataclass
class KeyValue:
    """A key/value field detected on a page.

    Attributes:
        key: The label tokens (e.g. ``[Word("Invoice"), Word("Number")]``).
        bbox: Normalized location of the key (label) on the page.
        page: 1-based page number the field was found on.
        confidence: Extraction confidence in ``[0.0, 1.0]``.
        value: The extracted value text, when known.
    """

    key: List[Word]
    bbox: BBox
    page: int
    confidence: float = 1.0
    value: str = ""

    @property
    def key_text(self) -> str:
        """The label as a single trimmed string."""
        return " ".join(w.text for w in self.key).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": [w.text for w in self.key],
            "bbox": self.bbox.to_dict(),
            "page": self.page,
            "confidence": self.confidence,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeyValue":
        return cls(
            key=[Word(t) for t in data["key"]],
            bbox=BBox.from_dict(data["bbox"]),
            page=int(data["page"]),
            confidence=float(data.get("confidence", 1.0)),
            value=str(data.get("value", "")),
        )


@dataclass
class Page:
    """A single page: its key/value fields plus whether it has a text layer."""

    key_values: List[KeyValue] = field(default_factory=list)
    has_text_layer: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_values": [kv.to_dict() for kv in self.key_values],
            "has_text_layer": self.has_text_layer,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Page":
        return cls(
            key_values=[KeyValue.from_dict(kv) for kv in data.get("key_values", [])],
            has_text_layer=bool(data.get("has_text_layer", False)),
        )


@dataclass
class Document:
    """A processed document: ordered pages of key/value fields."""

    pages: List[Page] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def is_born_digital(self) -> bool:
        """True when every page carries an extractable text layer."""
        return self.page_count > 0 and all(p.has_text_layer for p in self.pages)

    def all_key_values(self) -> List[KeyValue]:
        return [kv for page in self.pages for kv in page.key_values]

    def to_dict(self) -> Dict[str, Any]:
        return {"pages": [p.to_dict() for p in self.pages]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        return cls(pages=[Page.from_dict(p) for p in data.get("pages", [])])
