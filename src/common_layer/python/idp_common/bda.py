# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Amazon Bedrock Data Automation (BDA) request/response helpers (paid tier).

BDA is the managed escalation tier for documents that cheap OCR can't handle
confidently — scanned images, charts/plots, and visually complex layouts. It
returns structured fields *with* bounding boxes (visual grounding), which lets
this pipeline **train spatial memory** from BDA output: once a layout is
learned, future documents of the same type resolve for free from the OCR +
spatial path, and never hit BDA again.

Everything here is pure (no AWS calls) so it is unit-tested with recorded BDA
output JSON. The handler wraps these with the ``bedrock-data-automation-runtime``
client and S3.

BDA "custom output" (a blueprint) returns, per document:

    {
      "inference_result": { "<field>": "<value>", ... },
      "explainability_info": [
        { "<field>": {"success": true, "confidence": 0.97,
                       "geometry": [{"boundingBox": {"left","top","width","height"},
                                     "page": 0}]}, ... }
      ]
    }

Bounding boxes are already normalized to ``[0, 1]``; page indexes are 0-based
and converted to the 1-based convention used by :class:`~idp_common.models`.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from idp_common.models import BBox, Document, KeyValue, Page, Word


def _humanize(field_name: str) -> str:
    """Turn a snake/lower field name into a short instruction phrase."""
    return field_name.replace("_", " ").strip().lower()


# Common cross-industry metadata that most business documents carry, regardless
# of pack. Included in every blueprint (unless already named by the pack) so
# scanned/visual documents routed to BDA don't lose basic metadata just because
# a pack under-specifies its field list — BDA extracts strictly the properties
# named in its schema, with no "extract anything else you find" fallback like
# the LLM prompt has. Declared ``inferred`` (best-effort) rather than
# ``explicit``, since not every document type has all of these.
CATCH_ALL_FIELDS = {
    "document_date": "The primary date printed on the document, if any.",
    "reference_number": "Any document/report/reference number printed on the document.",
    "issuing_organization": "The company, agency, or organization that issued the document.",
    "prepared_by": "The person or role who authored, signed, or prepared the document.",
}


# Blueprint fields BDA infers or generates rather than reads from a printed
# label: the catch-all metadata plus the generated ``summary``. BDA often
# returns them without a bounding box, so they are left out of the
# template-trust check (threat T2) unless the pack itself names the field.
GENERATED_FIELDS = frozenset(CATCH_ALL_FIELDS) | {"summary"}


def trust_provenance(
    field_provenance: Dict[str, Any], pack_fields: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Field provenance that counts toward template trust for a BDA document.

    Drops :data:`GENERATED_FIELDS` that the pack does not declare itself. Every
    pack field stays, so a pack value without a bounding box still blocks
    trust. Only the trust check uses this; compliance and grounding see the
    full provenance.
    """
    declared = set(pack_fields or ())
    return {
        name: prov
        for name, prov in field_provenance.items()
        if name not in GENERATED_FIELDS or name in declared
    }


def build_blueprint_schema(
    field_names: List[str],
    class_name: str = "Document",
    description: str = "Fields for the cost-aware IDP sample.",
    field_descriptions: Optional[Dict[str, str]] = None,
    include_catch_all: bool = True,
) -> Dict[str, Any]:
    """Build a BDA document blueprint schema (draft-07) from field names.

    Each named field is declared ``inferenceType: explicit`` (extract the value
    verbatim). ``field_descriptions`` lets a pack author supply a description or
    synonyms per field name, which sharpens the extraction instruction beyond a
    generic humanized field name (e.g. "also called design load or allowable
    load"). When ``include_catch_all`` is set (default), a small set of common
    cross-industry metadata fields (:data:`CATCH_ALL_FIELDS`) is added as
    ``inferred`` properties, so a scanned/visual document doesn't lose basic
    metadata just because the pack's field list is narrower than the document.
    A generic ``summary`` (``inferred``) is always appended.

    BDA returns each field's value plus bounding boxes in
    ``explainability_info``, which is what :func:`parse_result` consumes. The
    field list typically comes from the active industry pack
    (:meth:`IndustryPack.fields_for`) so the visual tier extracts what
    reconcile/compliance expects, plus the catch-all metadata.
    """
    field_descriptions = field_descriptions or {}
    properties: Dict[str, Any] = {}
    for name in field_names:
        hint = field_descriptions.get(name)
        instruction = (
            f"The value of '{_humanize(name)}' if present. {hint}".strip()
            if hint
            else f"The value of '{_humanize(name)}' if present."
        )
        properties[name] = {
            "type": "string",
            "inferenceType": "explicit",
            "instruction": instruction,
        }
    if include_catch_all:
        for name, instruction in CATCH_ALL_FIELDS.items():
            properties.setdefault(
                name,
                {"type": "string", "inferenceType": "inferred", "instruction": instruction},
            )
    # Always include a short free-text summary as an inferred field.
    properties.setdefault(
        "summary",
        {
            "type": "string",
            "inferenceType": "inferred",
            "instruction": "A one-sentence summary of the document.",
        },
    )
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "description": description,
        "class": class_name,
        "type": "object",
        "properties": properties,
    }


def build_input_config(source_uri: str) -> Dict[str, str]:
    """Input config for ``invoke_data_automation_async``."""
    return {"s3Uri": source_uri}


def build_output_config(output_uri: str) -> Dict[str, str]:
    """Output config (a scratch S3 prefix BDA writes results under)."""
    return {"s3Uri": output_uri}


def build_automation_config(project_arn: str, stage: str = "LIVE") -> Dict[str, str]:
    """Data-automation config selecting the project + stage."""
    return {"dataAutomationProjectArn": project_arn, "stage": stage}


def profile_arn(region: str, account_id: str) -> str:
    """Construct the required ``dataAutomationProfileArn`` for a region/account."""
    return (
        f"arn:aws:bedrock:{region}:{account_id}:"
        "data-automation-profile/us.data-automation-v1"
    )


@dataclass
class ParsedBda:
    """Structured view of a BDA result: values, confidences, and a Document."""

    fields: Dict[str, str] = field(default_factory=dict)
    confidences: Dict[str, float] = field(default_factory=dict)
    document: Document = field(default_factory=Document)

    def confidence_for(self, name: str, default: float = 0.9) -> float:
        return self.confidences.get(name, default)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _bbox_from_geometry(geometry: List[Dict[str, Any]]) -> Optional[BBox]:
    """Take the first geometry entry's bounding box, if present."""
    if not geometry:
        return None
    box = (geometry[0] or {}).get("boundingBox") or {}
    if not box:
        return None
    return BBox(
        x=_clamp(float(box.get("left", 0.0))),
        y=_clamp(float(box.get("top", 0.0))),
        width=_clamp(float(box.get("width", 1e-4)), 1e-4, 1.0),
        height=_clamp(float(box.get("height", 1e-4)), 1e-4, 1.0),
    )


def _page_from_geometry(geometry: List[Dict[str, Any]]) -> int:
    if not geometry:
        return 1
    return int((geometry[0] or {}).get("page", 0)) + 1  # 0-based -> 1-based


def matched_class(result: Dict[str, Any]) -> Optional[str]:
    """Best-effort: the document class/type BDA matched to a blueprint.

    With multiple blueprints, BDA matches each document to the best-fitting one;
    the result identifies the match. The exact key has varied across BDA
    versions, so we check the common locations and return the first found (the
    blueprint's ``class`` is set to the document-type name). Returns ``None`` if
    no match indicator is present, so the caller can fall back to ``unknown``.
    """
    if not isinstance(result, dict):
        return None
    candidates = [
        (result.get("matched_blueprint") or {}).get("name"),
        (result.get("document_class") or {}).get("type"),
        (result.get("blueprint") or {}).get("name"),
        result.get("blueprint_name"),
        result.get("class"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def resolve_doc_type(result: Dict[str, Any], type_names: List[str]) -> Optional[str]:
    """Map the matched blueprint back to a pack document-type name.

    BDA reports the matched blueprint's ``name`` (e.g.
    ``<app>-<pack>-pile-test-report-doc``). We recover the clean type by matching
    the name's suffix against the pack's document types (their names hyphenated
    the same way the blueprint names are built). Falls back to a direct match on
    the class/type if BDA reports that instead. Returns ``None`` when nothing
    matches, so the caller can default to ``unknown``.
    """
    raw = matched_class(result)
    if not raw or not type_names:
        return None
    if raw in type_names:
        return raw
    for name in type_names:
        safe = re.sub(r"[^A-Za-z0-9-]", "-", name)
        if raw.endswith(f"-{safe}-doc") or raw == safe:
            return name
    return None


def parse_result(result: Dict[str, Any]) -> ParsedBda:
    """Parse a BDA custom-output result into fields + a spatial Document.

    Tolerant of missing ``explainability_info``: fields without geometry still
    appear in ``fields`` (so nothing is lost) but contribute no spatial box.
    """
    inference = result.get("inference_result") or {}
    explain_list = result.get("explainability_info") or []
    # explainability_info is a list (one entry per segment); merge them.
    explain: Dict[str, Any] = {}
    for entry in explain_list:
        if isinstance(entry, dict):
            explain.update(entry)

    parsed = ParsedBda()
    kvs_by_page: Dict[int, List[KeyValue]] = {}
    max_page = 1

    for name, value in inference.items():
        str_value = "" if value is None else str(value)
        parsed.fields[str(name)] = str_value

        info = explain.get(name) or {}
        confidence = float(info.get("confidence", 0.9))
        parsed.confidences[str(name)] = confidence

        geometry = info.get("geometry") or []
        bbox = _bbox_from_geometry(geometry)
        if bbox is None:
            continue
        page = _page_from_geometry(geometry)
        max_page = max(max_page, page)
        kvs_by_page.setdefault(page, []).append(
            KeyValue(
                key=[Word(tok) for tok in str(name).split()],
                bbox=bbox,
                page=page,
                confidence=confidence,
                value=str_value,
            )
        )

    parsed.document = Document(
        pages=[
            Page(key_values=kvs_by_page.get(p, []), has_text_layer=True)
            for p in range(1, max_page + 1)
        ]
    )
    return parsed
