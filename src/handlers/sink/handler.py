# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sink Lambda — emits the merged record to Amazon Data Firehose.

Firehose buffers and writes to the managed Apache Iceberg table (S3 Tables).
We do not write Iceberg directly: Firehose handles buffering, retries, and
table routing. The record is sent as a single newline-terminated JSON object.

After delivery the sink marks the document's dedup claim DONE, so only
successfully completed documents count as duplicates.
"""

import json
import logging
import os
from typing import Any, Dict

import boto3

from idp_common import annotations
from idp_common.dedup import DedupIndex
from idp_common.record import flatten_for_sink

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_firehose = boto3.client("firehose")
_s3 = boto3.client("s3")
DELIVERY_STREAM_NAME = os.environ.get("DELIVERY_STREAM_NAME", "")
ANNOTATIONS_ENABLED = os.environ.get("ANNOTATIONS_ENABLED", "false").lower() == "true"
DEDUP_TABLE = os.environ.get("DEDUP_TABLE", "")


def _maybe_annotate(event: Dict[str, Any]) -> bool:
    """Best-effort optional object annotation; never fails the pipeline."""
    if not ANNOTATIONS_ENABLED:
        return False
    location = annotations.parse_s3_uri(event.get("source_uri", ""))
    if location is None:
        return False
    try:
        return annotations.write_annotations(
            _s3,
            location[0],
            location[1],
            annotations.build_object_annotations(event),
            enabled=True,
        )
    except Exception:  # pragma: no cover - best-effort, non-fatal
        logger.exception("Annotation write failed (non-fatal)")
        return False


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    # The sink receives the merged DocumentRecord dict from the Reconcile step
    # and flattens it to the scalar/JSON row shape of the Iceberg table.
    row = flatten_for_sink(event)
    payload = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    _firehose.put_record(
        DeliveryStreamName=DELIVERY_STREAM_NAME,
        Record={"Data": payload.encode("utf-8")},
    )
    annotated = _maybe_annotate(event)
    doc_id = event.get("doc_id")
    logger.info("Sunk record for doc %s to %s", doc_id, DELIVERY_STREAM_NAME)
    dedup_completed = _mark_completed(event)
    return {
        "sunk": True,
        "doc_id": doc_id,
        "doc_type": event.get("doc_type"),
        "annotated": annotated,
        "dedup_completed": dedup_completed,
    }


def _mark_completed(event: Dict[str, Any]) -> bool:
    """Mark the preprocess dedup claim DONE so the hash counts as a duplicate.

    Non-fatal: the record is already delivered. If this fails, the claim's
    lease lapses and a later identical upload is simply processed again.
    """
    digest = event.get("content_hash")
    doc_id = event.get("doc_id")
    if not (DEDUP_TABLE and digest and doc_id):
        return False
    try:
        return DedupIndex(DEDUP_TABLE).complete(
            digest, doc_id, event.get("template_id")
        )
    except Exception:  # noqa: BLE001 - best-effort, non-fatal
        logger.exception("Dedup completion failed for doc %s (non-fatal)", doc_id)
        return False
