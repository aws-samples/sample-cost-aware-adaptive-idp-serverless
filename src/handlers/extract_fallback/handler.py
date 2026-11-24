# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock fallback Lambda — Tier 4 (most expensive, AI only when needed).

Reached only when the structured Textract tier still could not extract the
document. Uses the Bedrock Converse API, sending the source directly — no
rasterizing:

* Images go in as an ``image`` block.
* PDFs (and other documents) go in as a ``document`` block — Bedrock reads the
  PDF bytes directly, so no PDF-to-image rendering is needed.
* Text fallback over whatever OCR text exists, when neither applies.

Produces a sink-ready record with per-field provenance ``method=llm``. The LLM
output itself carries no positions, but the upstream Textract OCR document does
— so this handler learns that OCR layout into spatial memory, letting future
documents of the same type resolve on the free OCR + spatial path.

Template poisoning (threat T2): a learned layout starts provisional and only
becomes trusted after ``TEMPLATE_CONFIRMATIONS`` AI-checked documents agree on
it. When the OCR tier spot-checked a trusted template (``spot_check``), this
handler compares its own result with the template path and demotes the
template on disagreement. The AI result is the record either way.

Prompt injection (threat T1): with a guardrail configured, the document's OCR
text is screened with ``ApplyGuardrail`` before the model is called, in every
mode. Our own instruction and system prompt are never screened. A blocked
document is not sent to the model and is recorded with no fields.
"""

import logging
import os
from typing import Any, Dict, List

import boto3

from idp_common import bedrock, grounding
from idp_common.models import Document
from idp_common.packs import resolve_pack
from idp_common.record import METHOD_LLM, DocumentRecord
from idp_common.template_memory import DynamoDBTemplateStore, TemplateMemory, compare

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
TEMPLATE_TABLE = os.environ.get("TEMPLATE_TABLE", "")
PACK_NAME = os.environ.get("PACK_NAME", "construction")
PACKS_TABLE = os.environ.get("PACKS_TABLE", "")
LLM_CONFIDENCE = float(os.environ.get("LLM_CONFIDENCE", "0.8"))
_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))
# Optional Bedrock Guardrail (prompt-attack filter); empty = disabled.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "")
# AI-checked documents that must agree before a learned template is trusted (T2).
TEMPLATE_CONFIRMATIONS = max(1, int(os.environ.get("TEMPLATE_CONFIRMATIONS", "2")))

# Packs are runtime config in DynamoDB (source of truth), with the bundled YAML
# as fallback. Cache the resolved pack across invocations in the same container.
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        try:
            _PACK = resolve_pack(PACK_NAME, table_name=PACKS_TABLE or None)
        except Exception:  # pragma: no cover - degrade to generic extraction
            logger.exception("Could not resolve pack %s; extracting generically", PACK_NAME)
            _PACK = False  # sentinel: tried and failed
    return _PACK or None


def _learn_ocr_layout(
    event: Dict[str, Any], doc_type=None, ai_fields=None, field_provenance=None
) -> Dict[str, Any]:
    """Train spatial memory from the upstream OCR document (has bboxes).

    Stamps the classified ``doc_type`` on the template so future documents that
    resolve on the free spatial path inherit the same pack document type. The
    template stays provisional until enough AI-checked documents with every
    value grounded (``field_provenance``) agree. When
    this document was a spot-check of a trusted template, compare first and
    demote the template on disagreement. Returns the output keys to merge.
    """
    result: Dict[str, Any] = {"template_id": None, "template_state": None}
    if not TEMPLATE_TABLE:
        return result
    doc = Document.from_dict(event.get("document", {"pages": []}))
    try:
        memory = TemplateMemory(store=DynamoDBTemplateStore(TEMPLATE_TABLE))
        spot_id = event.get("spot_check_template_id")
        if event.get("spot_check") and spot_id:
            template = memory.get_template(spot_id)
            if template is not None:
                agree, reason = compare(doc, template.doc_type, doc_type, ai_fields or {})
                if not agree:
                    memory.demote(spot_id)
                    logger.warning("Spot-check demoted template %s: %s", spot_id, reason)
                result["spot_check_result"] = "agree" if agree else "disagree"
        if doc.all_key_values():
            # Only a document whose kept values were all grounded in the OCR
            # text counts toward template trust (threat T2).
            template_id, state = memory.learn_from_ai(
                doc,
                doc_type=doc_type,
                confirmations_required=TEMPLATE_CONFIRMATIONS,
                verified=grounding.all_verified(field_provenance or {}),
            )
            result["template_id"] = template_id
            result["template_state"] = state
    except Exception:  # pragma: no cover - learning is best-effort
        logger.exception("Spatial learning from OCR layout failed")
    return result


def _document_text(event: Dict[str, Any]) -> str:
    doc = Document.from_dict(event.get("document", {"pages": []}))
    lines = []
    for kv in doc.all_key_values():
        lines.append(f"{kv.key_text}: {kv.value}".strip())
    return "\n".join(lines)


def _build_content(
    bucket: str, key: str, event: Dict[str, Any], type_fields=None, field_descriptions=None
):
    """Choose the cheapest faithful Converse input for the source object.

    Images go in as an ``image`` block; PDFs and other documents go in as a
    ``document`` block (sent directly — no rasterizing). Anything else falls
    back to the OCR text we already have. When the active pack supplies per-type
    field lists (and optional per-field description/synonym hints), they are
    passed to the model so it classifies the document and extracts *that
    document type's* fields (and only those).
    """
    image_format = bedrock.image_format_for_key(key)
    if image_format is not None:
        body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return (
            bedrock.build_vision_content(
                body, image_format, type_fields=type_fields, field_descriptions=field_descriptions
            ),
            "vision",
        )

    doc_format = bedrock.document_format_for_key(key)
    if doc_format is not None:
        body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return (
            bedrock.build_document_content(
                body, doc_format, key, type_fields=type_fields, field_descriptions=field_descriptions
            ),
            "document",
        )

    return _text_content(event, type_fields, field_descriptions), "text"


def _text_content(event: Dict[str, Any], type_fields=None, field_descriptions=None):
    return bedrock.build_text_content(
        _document_text(event),
        type_fields=type_fields,
        field_descriptions=field_descriptions,
    )


def _guardrail_blocks(document_text: str) -> bool:
    """Screen the document's OCR text with the guardrail (ApplyGuardrail).

    Only the document text is sent, in every mode (text, document, vision), so
    our own extraction instruction and system prompt are never screened as
    user input: both read like instructions and the prompt-attack filter would
    block them. The Converse call itself carries no ``guardrailConfig``, so it
    screens nothing else. PDF and image bytes are not screened (the filter is
    text only); grounding still applies to the values read from them.
    """
    if not GUARDRAIL_ID or not document_text.strip():
        return False
    response = _bedrock.apply_guardrail(
        guardrailIdentifier=GUARDRAIL_ID,
        guardrailVersion=GUARDRAIL_VERSION or "DRAFT",
        source="INPUT",
        content=[{"text": {"text": document_text}}],
    )
    return response.get("action") == "GUARDRAIL_INTERVENED"


def _converse(content: List[Dict[str, Any]]) -> Dict[str, Any]:
    return _bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": content}],
        system=[{"text": bedrock.DEFAULT_SYSTEM}],
        inferenceConfig={"maxTokens": _MAX_TOKENS, "temperature": 0.0},
    )


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    bucket = event.get("bucket")
    key = event.get("key", "")
    logger.info("Bedrock fallback for doc %s (%s)", event.get("doc_id"), key)

    pack = _pack()
    doc_type_names = [dt.name for dt in pack.document_types] if pack else None
    # Per-type field lists: the model classifies, then extracts the chosen
    # type's fields (each includes the pack's linking keys via fields_for).
    type_fields = (
        {name: pack.fields_for(name) for name in doc_type_names} if pack else None
    )
    # Author-supplied per-field descriptions/synonyms, merged across every
    # document type (the prompt lists all types at once, so hints for any of
    # them may apply).
    field_descriptions = pack.descriptions_for() if pack else None

    if bucket:
        content, mode = _build_content(
            bucket, key, event, type_fields=type_fields, field_descriptions=field_descriptions
        )
    else:
        content, mode = _text_content(event, type_fields, field_descriptions), "text"

    # Screen the document text first; a blocked document never reaches the model.
    guardrail_intervened = _guardrail_blocks(_document_text(event))
    if guardrail_intervened:
        # No model call for a blocked document: no fields, no classification
        # and no template learning; reconcile sends it to review.
        logger.warning(
            "Bedrock guardrail intervened for doc %s; no fields extracted",
            event.get("doc_id"),
        )
        classified_type, fields = None, {}
    else:
        classified_type, fields = bedrock.parse_classified(
            bedrock.parse_converse_text(_converse(content)), valid_types=doc_type_names
        )
        if pack:
            # Only the active pack's field names may reach the record.
            fields = bedrock.restrict_fields(fields, pack.fields_for(classified_type))
    logger.info(
        "Bedrock (%s) classified=%s extracted %d field(s)",
        mode,
        classified_type or "unknown",
        len(fields),
    )

    record = DocumentRecord(
        doc_id=event["doc_id"],
        doc_type=classified_type or event.get("doc_type", "unknown"),
        source_uri=event.get("source_uri", f"s3://{bucket}/{key}"),
        content_hash=event.get("content_hash"),
    )
    # Ground every model value in the upstream OCR text under the same label;
    # an ungrounded value cannot make a compliance rule PASS on its own.
    index = grounding.ocr_index(Document.from_dict(event.get("document", {"pages": []})))
    for name, value in fields.items():
        # The classification prompt asks for empty strings on absent fields;
        # drop those so field/cost counts and compliance stay meaningful.
        if value is None or str(value).strip() == "":
            continue
        record.add_field(
            name=str(name),
            value=str(value),
            method=METHOD_LLM,
            confidence=LLM_CONFIDENCE,
            grounded=grounding.is_grounded(name, value, index),
        )

    out = record.to_dict()
    if guardrail_intervened:
        out["template_id"] = None
    else:
        out.update(
            _learn_ocr_layout(
                event,
                doc_type=classified_type,
                ai_fields=out["fields"],
                field_provenance=out["field_provenance"],
            )
        )
    out["extraction_method"] = METHOD_LLM
    out["llm_mode"] = mode
    if guardrail_intervened:
        out["guardrail_intervened"] = True
    return out
