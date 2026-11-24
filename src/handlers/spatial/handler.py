# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spatial Lambda — free template memory.

Classifies the document against *trusted* learned templates, refines the
matched template's regions so it self-refines, and assembles the merged
:class:`DocumentRecord` with per-field provenance.

This stage runs on the "clear OCR" path: the upstream Textract
``DetectDocumentText`` tier produced a document with bounding boxes, so the
values are OCR-derived (``method=textract``). As templates mature, more
documents resolve as "clear" and skip the paid escalation tiers entirely.

Template poisoning (threat T2): this free path has no AI check, so it never
creates a template and never changes a template's ``doc_type`` or trust state.
Templates are created only by the AI-checked tiers (Nova, BDA) and must be
confirmed before they are trusted; here an existing trusted template may only
have its regions refined.
"""

import logging
import os
from typing import Any, Dict

from idp_common.models import Document
from idp_common.template_memory import DynamoDBTemplateStore, TemplateMemory
from idp_common.record import METHOD_TEXTRACT, DocumentRecord

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TEMPLATE_TABLE = os.environ.get("TEMPLATE_TABLE", "")
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.7"))


def _build_memory() -> TemplateMemory:
    store = DynamoDBTemplateStore(TEMPLATE_TABLE)
    return TemplateMemory(store=store, similarity_threshold=SIMILARITY_THRESHOLD)


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    document = Document.from_dict(event["document"])
    memory = _build_memory()

    match = memory.identify_template(document, trusted_only=True)
    if match is not None:
        template_id = match.template_id
        memory.refine_from_spatial(document, template_id)
        # Inherit the pack document type the AI tier stamped on the template;
        # fall back to the template id for never-classified layouts.
        learned = memory.get_template(template_id)
        stored_type = learned.doc_type if learned is not None else None
        doc_type = stored_type or template_id
        template_state = "trusted"
    else:
        # No trusted template (e.g. the structured-Forms path): no learning.
        template_id = None
        template_state = None
        doc_type = memory.identifier.compute_signature(document)
    logger.info(
        "Spatial: doc %s -> template %s (matched=%s, doc_type=%s)",
        event.get("doc_id"),
        template_id,
        match is not None,
        doc_type,
    )

    # The method/tier is supplied by whichever upstream stage produced the
    # document (Textract basic OCR by default, or structured Forms when opt-in).
    extraction_method = event.get("extraction_method", METHOD_TEXTRACT)

    record = DocumentRecord(
        doc_id=event["doc_id"],
        doc_type=doc_type,
        source_uri=event["source_uri"],
        content_hash=event.get("content_hash"),
    )
    for kv in document.all_key_values():
        name = kv.key_text
        if not name:
            continue
        record.add_field(
            name=name,
            value=kv.value,
            method=extraction_method,
            confidence=kv.confidence,
            region_bbox=kv.bbox.to_dict(),
        )

    out = record.to_dict()
    out["template_id"] = template_id
    out["template_matched"] = match is not None
    out["template_state"] = template_state
    return out
