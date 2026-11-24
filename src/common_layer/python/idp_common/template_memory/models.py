# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data models for spatial field-location memory.

A :class:`FieldLocationMap` is the persisted "template": for one document
type, it records each field name and the bounding box(es) where it has been
observed. New observations are merged in with a decayed weighted average so
positions self-refine as more documents of the same type are seen.

Each map also carries a trust state (threat T2, template poisoning). A
template starts ``provisional`` and only becomes ``trusted`` after enough
AI-checked documents agree on its layout and document type; only trusted
templates may resolve documents on the free path.
"""

import datetime
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


TRUST_PROVISIONAL = "provisional"
TRUST_TRUSTED = "trusted"


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class FieldRegion:
    """A recorded region where a field was observed.

    Attributes:
        field_name: Non-empty field label.
        page: 1-based page number.
        bbox: Normalized box dict with keys ``x``, ``y``, ``width``, ``height``.
        confidence: Observation confidence in ``[0.0, 1.0]``.
        occurrence_count: How many observations are folded into this region.
    """

    field_name: str
    page: int
    bbox: Dict[str, float]
    confidence: float
    occurrence_count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.field_name, str) or not self.field_name:
            raise ValueError(f"field_name must be non-empty, got: {self.field_name!r}")
        if not isinstance(self.page, int) or self.page < 1:
            raise ValueError(f"page must be an int >= 1, got: {self.page!r}")
        required = {"x", "y", "width", "height"}
        missing = required - set(self.bbox)
        if missing:
            raise ValueError(f"bbox missing keys: {missing}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0,1], got: {self.confidence}")
        if not isinstance(self.occurrence_count, int) or self.occurrence_count < 1:
            raise ValueError(
                f"occurrence_count must be int >= 1, got: {self.occurrence_count!r}"
            )

    @property
    def center(self) -> tuple:
        return (
            self.bbox["x"] + self.bbox["width"] / 2.0,
            self.bbox["y"] + self.bbox["height"] / 2.0,
        )


@dataclass
class FieldLocationMap:
    """All known field regions for a single document template."""

    template_id: str
    fields: Dict[str, List[FieldRegion]] = field(default_factory=dict)
    page_count: int = 1
    sample_count: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # The industry-pack document type this layout was classified as, stamped by
    # the LLM/BDA tier when the template was learned. The free spatial path reads
    # it back so repeat documents inherit the same doc_type.
    doc_type: Optional[str] = None
    # Trust state (threat T2): "provisional" until ``confirmations`` AI-checked
    # documents agree; legacy items without these keys load as provisional/0.
    trust_state: str = TRUST_PROVISIONAL
    confirmations: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or not self.template_id:
            raise ValueError("template_id must be a non-empty string")

    def add_field(self, field_name: str, region: FieldRegion) -> None:
        self.fields.setdefault(field_name, []).append(region)

    def get_field_region(
        self, field_name: str, page: Optional[int] = None
    ) -> Optional[FieldRegion]:
        """Best (most-observed) region for a field, optionally on one page."""
        regions = self.fields.get(field_name)
        if not regions:
            return None
        candidates = [r for r in regions if page is None or r.page == page]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.occurrence_count)

    def merge(
        self, other: "FieldLocationMap", tolerance: float, decay_factor: float = 1.0
    ) -> None:
        """Fold ``other``'s regions into this map using decayed averaging.

        Regions within ``tolerance`` (center distance) of an existing region of
        the same field+page are averaged together; otherwise added as new.
        """
        if not 0.5 <= decay_factor <= 1.0:
            raise ValueError(f"decay_factor must be in [0.5, 1.0], got: {decay_factor}")
        if self.template_id != other.template_id:
            raise ValueError("cannot merge maps with different template_ids")

        self.sample_count += 1
        self.updated_at = _utc_now()

        for field_name, regions in other.fields.items():
            for new_region in regions:
                if not self._merge_region(field_name, new_region, tolerance, decay_factor):
                    self.fields.setdefault(field_name, []).append(new_region)

    def _merge_region(
        self,
        field_name: str,
        new_region: FieldRegion,
        tolerance: float,
        decay_factor: float,
    ) -> bool:
        for existing in self.fields.get(field_name, []):
            if existing.page != new_region.page:
                continue
            ex_cx, ex_cy = existing.center
            new_cx, new_cy = new_region.center
            distance = math.hypot(ex_cx - new_cx, ex_cy - new_cy)
            if distance > tolerance:
                continue
            weight = max(0.01, existing.occurrence_count * decay_factor)
            total = weight + 1.0
            for k in ("x", "y", "width", "height"):
                existing.bbox[k] = (existing.bbox[k] * weight + new_region.bbox[k]) / total
            existing.confidence = (
                existing.confidence * weight + new_region.confidence
            ) / total
            existing.occurrence_count += 1
            return True
        return False

    # ----- serialization -------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "page_count": self.page_count,
            "sample_count": self.sample_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "doc_type": self.doc_type,
            "trust_state": self.trust_state,
            "confirmations": self.confirmations,
            "fields": {
                name: [
                    {
                        "field_name": r.field_name,
                        "page": r.page,
                        "bbox": dict(r.bbox),
                        "confidence": r.confidence,
                        "occurrence_count": r.occurrence_count,
                    }
                    for r in regions
                ]
                for name, regions in self.fields.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FieldLocationMap":
        fields: Dict[str, List[FieldRegion]] = {}
        for name, region_list in data.get("fields", {}).items():
            fields[name] = [
                FieldRegion(
                    field_name=r["field_name"],
                    page=int(r["page"]),
                    bbox={k: float(v) for k, v in r["bbox"].items()},
                    confidence=float(r["confidence"]),
                    occurrence_count=int(r.get("occurrence_count", 1)),
                )
                for r in region_list
            ]
        return cls(
            template_id=data["template_id"],
            fields=fields,
            page_count=int(data.get("page_count", 1)),
            sample_count=int(data.get("sample_count", 1)),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            doc_type=data.get("doc_type"),
            trust_state=(
                TRUST_TRUSTED
                if data.get("trust_state") == TRUST_TRUSTED
                else TRUST_PROVISIONAL
            ),
            confirmations=int(data.get("confirmations", 0) or 0),
        )
