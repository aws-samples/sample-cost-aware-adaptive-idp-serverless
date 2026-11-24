# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Optional object-context adapter (S3 Annotations), default OFF.

When enabled, attaches queryable context (doc type, entity keys, compliance,
compression algo, extraction id) directly to each source object, so the
context travels with the object through archival/copy/cross-region.

This is a *toggleable* adapter: when disabled it is a strict no-op, keeping the
pipeline portable and the demo minimal. The write is intentionally isolated in
:func:`write_annotations` so the concrete backend can be swapped — this sample
uses S3 object tagging as a portable backend; native S3 Annotations can
replace the single ``put_object_tagging`` call where the feature is available.

The payload builder :func:`build_object_annotations` is pure and unit-tested.
"""

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# S3 object tagging limits (portable backend).
_MAX_TAGS = 10
_MAX_KEY_LEN = 128
_MAX_VALUE_LEN = 256


def build_object_annotations(record: Dict[str, Any]) -> Dict[str, str]:
    """Build the object-context key/values from a merged record."""
    annotations: Dict[str, str] = {}
    if record.get("doc_type"):
        annotations["doc_type"] = str(record["doc_type"])
    if record.get("compliance_result"):
        # Keep just the overall status (PASS/FAIL/NA) to stay within limits.
        annotations["compliance"] = str(record["compliance_result"]).split(" ", 1)[0]
    if record.get("compression_algo"):
        annotations["compression_algo"] = str(record["compression_algo"])
    if record.get("doc_id"):
        annotations["extraction_id"] = str(record["doc_id"])
    if record.get("paid_field_count") is not None:
        annotations["paid_field_count"] = str(record["paid_field_count"])
    for key, value in (record.get("entity_keys") or {}).items():
        annotations[key] = str(value)
    return _sanitize(annotations)


def _sanitize(annotations: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in list(annotations.items())[:_MAX_TAGS]:
        out[str(key)[:_MAX_KEY_LEN]] = str(value)[:_MAX_VALUE_LEN]
    return out


def parse_s3_uri(source_uri: str) -> Optional[Tuple[str, str]]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``; None if not an S3 URI."""
    parsed = urlparse(source_uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        return None
    return parsed.netloc, parsed.path.lstrip("/")


def write_annotations(
    s3_client,
    bucket: str,
    key: str,
    annotations: Dict[str, str],
    enabled: bool,
) -> bool:
    """Attach annotations to the object. No-op (returns False) when disabled.

    Returns True if annotations were written.
    """
    if not enabled or not annotations or not bucket or not key:
        return False
    tag_set = [{"Key": k, "Value": v} for k, v in annotations.items()]
    s3_client.put_object_tagging(
        Bucket=bucket, Key=key, Tagging={"TagSet": tag_set}
    )
    logger.info("Annotated s3://%s/%s with %d tag(s)", bucket, key, len(tag_set))
    return True
