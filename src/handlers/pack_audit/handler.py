# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pack-audit Lambda — alerts on every change to the packs table.

Triggered by the PacksTable DynamoDB stream (``NEW_AND_OLD_IMAGES``). Pack
definitions are runtime config: an edit to a compliance rule takes effect on
the next reconcile cold start with no deploy. This handler makes every such
edit visible by publishing one SNS alert per change with:

  * the pack name and stream event (INSERT / MODIFY / REMOVE),
  * the old and new pack content hash (the same fingerprint reconcile stamps
    into each record's ``compliance_result``), and
  * the compliance rule ids added, removed or changed (expression differs).

The alert carries metadata only (rule ids, hashes), never document field
values. It does not identify who made the change: stream records carry no
caller identity.
"""

import json
import logging
import os
from typing import Any, Dict, Optional

import boto3

from idp_common.packs import pack_fingerprint

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sns = boto3.client("sns")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")


def _definition(image: Optional[Dict[str, Any]]):
    """Parse ``definition.S`` from a stream image. Returns ``(dict|None, ok)``."""
    if not image:
        return None, True
    raw = (image.get("definition") or {}).get("S")
    if raw is None:
        return None, False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, False
    return (data, True) if isinstance(data, dict) else (None, False)


def _rules(data: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{rule_id: expr}`` for pack-wide rules and ``<doc_type>/<id>`` for typed ones."""
    out: Dict[str, str] = {}
    if not data:
        return out
    for rule in data.get("compliance_rules") or []:
        if isinstance(rule, dict) and "id" in rule:
            out[str(rule["id"])] = str(rule.get("expr", ""))
    for dt in data.get("document_types") or []:
        if not isinstance(dt, dict):
            continue
        for rule in dt.get("compliance_rules") or []:
            if isinstance(rule, dict) and "id" in rule:
                out[f"{dt.get('name', '?')}/{rule['id']}"] = str(rule.get("expr", ""))
    return out


def _hash(data: Optional[Dict[str, Any]], ok: bool, present: bool) -> Optional[str]:
    if not present:
        return None
    if not ok or data is None:
        return "invalid"
    return pack_fingerprint(data)


def summarize_change(record: Dict[str, Any]) -> Dict[str, Any]:
    """Pure diff of one DynamoDB stream record into an alert summary."""
    ddb = record.get("dynamodb") or {}
    old_image = ddb.get("OldImage")
    new_image = ddb.get("NewImage")
    pack_name = ((ddb.get("Keys") or {}).get("pack_name") or {}).get("S", "unknown")

    old_data, old_ok = _definition(old_image)
    new_data, new_ok = _definition(new_image)
    old_rules, new_rules = _rules(old_data), _rules(new_data)

    return {
        "event": "pack_changed",
        "event_name": record.get("eventName", "UNKNOWN"),
        "pack_name": pack_name,
        "old_content_hash": _hash(old_data, old_ok, old_image is not None),
        "new_content_hash": _hash(new_data, new_ok, new_image is not None),
        "rules_added": sorted(set(new_rules) - set(old_rules)),
        "rules_removed": sorted(set(old_rules) - set(new_rules)),
        "rules_changed": sorted(
            rid for rid in set(old_rules) & set(new_rules) if old_rules[rid] != new_rules[rid]
        ),
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    alerts = 0
    for record in event.get("Records") or []:
        summary = summarize_change(record)
        logger.warning(json.dumps(summary))
        if ALERT_TOPIC_ARN:
            _sns.publish(
                TopicArn=ALERT_TOPIC_ARN,
                Subject=f"IDP pack changed: {summary['pack_name']}"[:100],
                Message=json.dumps(summary, indent=2),
            )
            alerts += 1
    return {"alerts": alerts}
