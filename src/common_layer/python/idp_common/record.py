# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The merged per-document record and its per-field provenance.

Every extracted field carries the tier/method that produced it, so cost
savings are provable in SQL: the free method (spatial memory) vs. the paid
managed services (textract OCR, llm, bda). This record is what the sink
writes to Iceberg.
"""

import datetime
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Allowed extraction methods, cheapest first. Mirrors the tier cascade:
# spatial memory replays learned layouts for free; every other method calls a
# paid managed service. The escalation tiers (llm, bda) also *train* spatial
# memory so future runs of the same layout resolve for free.
METHOD_SPATIAL = "spatial"  # free — learned template memory (applied cheaply)
METHOD_TEXTRACT = "textract"  # paid — basic DetectDocumentText OCR (default tier)
METHOD_TEXTRACT_CUSTOM = "textract_custom"  # paid — structured FORMS/TABLES (opt-in)
METHOD_TABLE = "table"  # paid — structured table extraction (opt-in)
METHOD_LLM = "llm"  # paid — cheap LLM (Nova) text structuring
METHOD_BDA = "bda"  # paid — Bedrock Data Automation (image/chart/hard docs)

FREE_METHODS = frozenset({METHOD_SPATIAL})


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def flatten_for_sink(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a rich record dict into the scalar/JSON Iceberg sink row.

    Complex structures (fields, provenance) become JSON strings so the
    Firehose -> Iceberg mapping stays flat, while scalar cost columns
    (free vs paid field counts) remain directly queryable in Athena.
    """
    import json

    fields = rec.get("fields", {}) or {}
    provenance = rec.get("field_provenance", {}) or {}
    method_summary = rec.get("extraction_method_summary") or {}
    free = sum(
        1 for p in provenance.values() if p.get("method") in FREE_METHODS
    )
    paid = rec.get("paid_field_count")
    if paid is None:
        paid = len(provenance) - free
    return {
        "doc_id": rec.get("doc_id"),
        "doc_type": rec.get("doc_type"),
        "source_uri": rec.get("source_uri"),
        "content_hash": rec.get("content_hash"),
        "ingested_at": rec.get("ingested_at") or _utc_now(),
        "mean_confidence": float(rec.get("mean_confidence", 0.0)),
        "field_count": len(fields),
        "free_field_count": free,
        "paid_field_count": paid,
        "method_summary_json": json.dumps(method_summary, ensure_ascii=False),
        "fields_json": json.dumps(fields, ensure_ascii=False),
        "field_provenance_json": json.dumps(provenance, ensure_ascii=False),
        "compliance_result": rec.get("compliance_result"),
        "compression_algo": rec.get("compression_algo", "none"),
    }


@dataclass
class FieldProvenance:
    """How a single field's value was obtained."""

    method: str
    confidence: float
    region_bbox: Optional[Dict[str, float]] = None
    # Model-output verification: True/False for AI tiers (value found in the
    # OCR text / BDA returned a box for it), None for deterministic tiers.
    grounded: Optional[bool] = None

    @property
    def is_free(self) -> bool:
        return self.method in FREE_METHODS


@dataclass
class DocumentRecord:
    """One record per document, merged across all regions/tiers."""

    doc_id: str
    doc_type: str
    source_uri: str
    fields: Dict[str, str] = field(default_factory=dict)
    field_provenance: Dict[str, FieldProvenance] = field(default_factory=dict)
    entity_keys: Dict[str, str] = field(default_factory=dict)
    compliance_result: Optional[str] = None
    compression_algo: str = "none"
    content_hash: Optional[str] = None
    ingested_at: str = field(default_factory=_utc_now)

    @property
    def mean_confidence(self) -> float:
        provs = list(self.field_provenance.values())
        if not provs:
            return 0.0
        return sum(p.confidence for p in provs) / len(provs)

    @property
    def method_summary(self) -> Dict[str, int]:
        """Count of fields per extraction method (drives cost analytics)."""
        summary: Dict[str, int] = {}
        for prov in self.field_provenance.values():
            summary[prov.method] = summary.get(prov.method, 0) + 1
        return summary

    @property
    def paid_field_count(self) -> int:
        return sum(1 for p in self.field_provenance.values() if not p.is_free)

    def add_field(
        self,
        name: str,
        value: str,
        method: str,
        confidence: float,
        region_bbox: Optional[Dict[str, float]] = None,
        grounded: Optional[bool] = None,
    ) -> None:
        self.fields[name] = value
        self.field_provenance[name] = FieldProvenance(
            method=method,
            confidence=confidence,
            region_bbox=region_bbox,
            grounded=grounded,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Flatten to a JSON-serializable dict suitable for the Iceberg sink."""
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "source_uri": self.source_uri,
            "fields": dict(self.fields),
            "field_provenance": {
                name: asdict(prov) for name, prov in self.field_provenance.items()
            },
            "entity_keys": dict(self.entity_keys),
            "extraction_method_summary": self.method_summary,
            "mean_confidence": round(self.mean_confidence, 6),
            "paid_field_count": self.paid_field_count,
            "compliance_result": self.compliance_result,
            "compression_algo": self.compression_algo,
            "content_hash": self.content_hash,
            "ingested_at": self.ingested_at,
        }
