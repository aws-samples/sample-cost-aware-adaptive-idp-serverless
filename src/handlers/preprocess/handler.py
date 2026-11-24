# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Preprocess Lambda — Tier 0 (free).

Responsibilities:
    1. Enforce the cost guardrails: reject files over ``MAX_FILE_BYTES`` (from
       object metadata, before reading the body) and PDFs over ``MAX_PAGES``,
       so oversize documents never reach a paid tier. Rejections are logged as
       a structured ``document_rejected`` line.
    2. Fetch the uploaded object from S3.
    3. Deduplicate by content hash (skip anything already completed or still
       in progress — the cheapest possible "extraction": no OCR, no AI), and
       claim the hash as in progress for this execution.
    4. Emit the routing contract for the state machine.

The state machine's failure path also invokes this function with
``{"action": "release_dedup", "content_hash", "doc_id"}`` to release the
claim, so a failed run does not block re-uploading the same file.

This handler intentionally does **no** content extraction. Every non-duplicate
document flows to cheap Textract ``DetectDocumentText`` OCR first (the default
tier); only when that is unclear does the pipeline escalate to a cheap LLM or
Bedrock Data Automation. Keeping preprocess free and dependency-light is core
to the "AI only when needed" cost model.

Output is the contract consumed by the Textract OCR step.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

import boto3

from idp_common import pdf_utils
from idp_common.dedup import DedupIndex, DedupResult, content_hash

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")
DEDUP_TABLE = os.environ.get("DEDUP_TABLE", "")
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(50 * 1024 * 1024)))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "200"))
# State-machine timeout (30 min) + 5 min margin.
DEDUP_LEASE_SECONDS = int(os.environ.get("DEDUP_LEASE_SECONDS", "2100"))

# Extensions Textract/Bedrock treat as single-page raster images.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".gif", ".webp")


def _resolve_source(event: Dict[str, Any]) -> Dict[str, str]:
    """Accept either a direct ``{bucket,key}`` or a raw EventBridge S3 event."""
    if "bucket" in event and "key" in event:
        return {"bucket": event["bucket"], "key": event["key"]}
    detail = event.get("detail", {})
    return {
        "bucket": detail["bucket"]["name"],
        "key": detail["object"]["key"],
    }


def _release(event: Dict[str, Any]) -> Dict[str, Any]:
    """Failure path: drop this execution's in-progress dedup claim."""
    digest = event.get("content_hash")
    doc_id = event.get("doc_id")
    released = False
    if DEDUP_TABLE and digest and doc_id:
        released = DedupIndex(DEDUP_TABLE).release(digest, doc_id)
    logger.info(
        "Dedup claim release for doc %s (hash %s): released=%s",
        doc_id,
        digest,
        released,
    )
    return {"released": released, "doc_id": doc_id, "content_hash": digest}


def _duplicate(
    existing: DedupResult, digest: str, source_uri: str
) -> Dict[str, Any]:
    logger.info(
        "Duplicate of %s (hash %s, in_progress=%s) — skipping",
        existing.first_doc_id,
        digest,
        existing.in_progress,
    )
    return {
        "duplicate": True,
        "in_progress": existing.in_progress,
        "rejected": False,
        "content_hash": digest,
        "source_uri": source_uri,
        "doc_id": existing.first_doc_id,
        "template_id": existing.template_id,
    }


def _reject(
    reason: str,
    *,
    bucket: str,
    key: str,
    source_uri: str,
    limit: int,
    size_bytes: int,
    page_count: Optional[int] = None,
    digest: Optional[str] = None,
) -> Dict[str, Any]:
    """Stop before any paid tier; log a structured line operators can alarm on."""
    logger.warning(
        json.dumps(
            {
                "event": "document_rejected",
                "reason": reason,
                "source_uri": source_uri,
                "size_bytes": size_bytes,
                "page_count": page_count,
                "limit": limit,
            }
        )
    )
    return {
        "duplicate": False,
        "rejected": True,
        "reason": reason,
        "doc_id": str(uuid.uuid4()),
        "source_uri": source_uri,
        "bucket": bucket,
        "key": key,
        "content_hash": digest,
        "size_bytes": size_bytes,
        "page_count": page_count,
    }


def _pdf_pages(key: str, body: bytes) -> Optional[int]:
    """Page count for PDFs, ``1`` for images, ``None`` if unknown."""
    if key.lower().endswith(_IMAGE_EXTENSIONS):
        return 1
    if not (key.lower().endswith(".pdf") or body.startswith(b"%PDF-")):
        return None
    try:
        return pdf_utils.page_count(body)
    except Exception:  # noqa: BLE001 - the size cap still bounds an unparseable PDF
        logger.warning("Could not count pages of %s; size limit still applies", key)
        return None


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    if event.get("action") == "release_dedup":
        return _release(event)

    source = _resolve_source(event)
    bucket, key = source["bucket"], source["key"]
    source_uri = f"s3://{bucket}/{key}"
    logger.info("Preprocessing %s", source_uri)

    size_bytes = int(_s3.head_object(Bucket=bucket, Key=key)["ContentLength"])
    if size_bytes > MAX_FILE_BYTES:
        return _reject(
            "file_too_large",
            bucket=bucket,
            key=key,
            source_uri=source_uri,
            limit=MAX_FILE_BYTES,
            size_bytes=size_bytes,
        )

    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    digest = content_hash(body)

    dedup = DedupIndex(DEDUP_TABLE) if DEDUP_TABLE else None
    if dedup is not None:
        existing = dedup.check(digest)
        if existing.is_duplicate or existing.in_progress:
            return _duplicate(existing, digest, source_uri)

    pages = _pdf_pages(key, body)
    if pages is not None and pages > MAX_PAGES:
        return _reject(
            "too_many_pages",
            bucket=bucket,
            key=key,
            source_uri=source_uri,
            limit=MAX_PAGES,
            size_bytes=size_bytes,
            page_count=pages,
            digest=digest,
        )

    doc_id = str(uuid.uuid4())
    if dedup is not None:
        claim = dedup.claim(digest, doc_id, source_uri, DEDUP_LEASE_SECONDS)
        if not claim.acquired:
            # Lost a race with a concurrent identical upload.
            return _duplicate(claim, digest, source_uri)

    is_image = key.lower().endswith(_IMAGE_EXTENSIONS)
    result = {
        "duplicate": False,
        "rejected": False,
        "doc_id": doc_id,
        "source_uri": source_uri,
        "bucket": bucket,
        "key": key,
        "content_hash": digest,
        "is_image": is_image,
    }
    logger.info("Preprocessed %s: is_image=%s", source_uri, is_image)
    return result
