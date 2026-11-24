# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reconcile Lambda — pack-driven linking + compliance (runs before the sink).

Every record (from any extraction path: spatial, Bedrock, or chart) flows
through here. Using the active industry pack we:

  * pull linking-key values into ``entity_keys`` so records can be reconciled
    across a project/asset lifecycle (joinable later in Athena), and
  * evaluate the pack's compliance rules and stamp ``compliance_result``,
    suffixed with ``[pack=<industry>@<content hash>]`` so each record can be
    traced to the exact pack definition that produced the verdict, then
    ``[template=<id>@<trust state>]`` and ``[spot_check=agree|disagree]``
    when a spatial template was involved (threat T2).

Cross-document reconciliation of conflicting values is provided as a pure
building block (:func:`idp_common.compliance.reconcile_records`) and surfaced
in the analytics layer; this per-document step does linking + compliance.
"""

import logging
import os
from typing import Any, Dict

from idp_common import compliance
from idp_common.packs import resolve_pack

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PACK_NAME = os.environ.get("PACK_NAME", "construction")
PACKS_TABLE = os.environ.get("PACKS_TABLE", "")

# Packs are runtime config in DynamoDB (source of truth), with the bundled YAML
# as fallback. Cache the resolved pack across invocations in the same container.
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        _PACK = resolve_pack(PACK_NAME, table_name=PACKS_TABLE or None)
    return _PACK


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    pack = _pack()
    fields = event.get("fields", {}) or {}
    doc_type = event.get("doc_type")

    entity_keys = compliance.extract_linking_keys(fields, pack)
    # Model values that could not be grounded (OCR text / BDA box) cannot make a
    # rule PASS on their own; a guardrail intervention sends every rule to review.
    unverified = [
        name
        for name, prov in (event.get("field_provenance") or {}).items()
        if isinstance(prov, dict) and prov.get("grounded") is False
    ]
    # Evaluate only the matched document type's rules (plus any pack-wide rules).
    report = compliance.evaluate_compliance(
        fields,
        pack,
        doc_type=doc_type,
        unverified_fields=unverified,
        force_review=bool(event.get("guardrail_intervened")),
    )

    event["entity_keys"] = entity_keys
    # Suffix the pack fingerprint so every record names the exact rule set that
    # judged it (the Iceberg schema is unchanged; the verdict stays first, so
    # ``LIKE 'FAIL%'`` queries keep working).
    summary = f"{report.to_summary()} [pack={pack.industry}@{pack.content_hash}]"
    # Template trust (threat T2): name the template behind the record and its
    # trust state, plus the spot-check outcome when one ran.
    template_id = event.get("template_id")
    if template_id:
        summary += f" [template={template_id}@{event.get('template_state') or 'unknown'}]"
    if event.get("spot_check_result"):
        summary += f" [spot_check={event['spot_check_result']}]"
    event["compliance_result"] = summary
    event["pack_content_hash"] = pack.content_hash
    logger.info(
        "Reconcile doc %s: pack=%s@%s linking_keys=%s compliance=%s",
        event.get("doc_id"),
        pack.industry,
        pack.content_hash,
        list(entity_keys.keys()),
        report.overall,
    )
    return event
