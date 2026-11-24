# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Textract durable Lambda — Tier 1 (cheap OCR) and Tier 2 (structured).

This is a Lambda **durable function**. A single handler serves both tiers,
selected by the ``TIER`` env var (``ocr`` or ``forms``).

For multi-page PDFs/TIFFs, Textract runs asynchronously. Instead of polling in
a sleep loop (which burns compute and is capped by the 15-minute Lambda
timeout), the durable handler uses ``context.wait(...)`` between status checks:
the function **suspends without compute charge** and resumes when the wait
elapses, with checkpoint/replay recovery. Single-page images use the
synchronous Textract APIs.

Step Functions invokes this function with the **task-token** integration
pattern (``WAIT_FOR_TASK_TOKEN``): the token is passed in the event, and on
completion the handler calls ``SendTaskSuccess`` with the result, so the
top-level Step Functions workflow resumes. Step Functions stays the
cross-service orchestrator; the durable function owns the long async wait.

The pure parsing/clarity logic lives in ``idp_common`` and is unit-tested;
``_build_output`` (below) is the unit-tested seam. Durable orchestration is
validated by synthesis and exercised at deploy time.

Template trust (threat T2): the OCR tier only counts *trusted* templates when
deciding whether a document is clear, and sends a ``SPOT_CHECK_PERCENT`` share
of clear documents to the AI tier anyway (``spot_check``) so a trusted template
keeps being compared against AI results.
"""

import json
import logging
import os
from typing import Any, Dict, List, Tuple

import boto3
from aws_durable_execution_sdk_python import (  # type: ignore
    DurableContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import Duration  # type: ignore

from idp_common import textract as tx
from idp_common.escalation import assess_ocr_clarity
from idp_common.record import METHOD_TEXTRACT
from idp_common.template_memory import (
    DynamoDBTemplateStore,
    TemplateMemory,
    select_for_spot_check,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_textract = boto3.client("textract")
_sfn = boto3.client("stepfunctions")
TIER = os.environ.get("TIER", "ocr")
TEMPLATE_TABLE = os.environ.get("TEMPLATE_TABLE", "")
ASYNC_EXTENSIONS = (".pdf", ".tiff", ".tif")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
_MAX_POLLS = int(os.environ.get("MAX_POLLS", "120"))  # durable waits are free
# Share (0-100) of trusted-template documents re-checked by the AI tier (T2).
SPOT_CHECK_PERCENT = int(os.environ.get("SPOT_CHECK_PERCENT", "5"))


# ----------------------------------------------------------------------
# Plain (mockable) helpers — no durable decorators, unit-testable.
# ----------------------------------------------------------------------
def _resolve_location(event: Dict[str, Any]) -> Tuple[str, str]:
    if "bucket" in event and "key" in event:
        return event["bucket"], event["key"]
    uri = event["source_uri"]  # s3://bucket/key
    _, _, rest = uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    return bucket, key


def _is_async(key: str) -> bool:
    return key.lower().endswith(ASYNC_EXTENSIONS)


def _features() -> List[str]:
    return ["FORMS", "TABLES"]


def _run_sync(bucket: str, key: str, tier: str) -> Dict[str, Any]:
    """Synchronous Textract for single-page images."""
    document = {"S3Object": {"Bucket": bucket, "Name": key}}
    if tier == "forms":
        return _textract.analyze_document(Document=document, FeatureTypes=_features())
    return _textract.detect_document_text(Document=document)


def _start_async(bucket: str, key: str, tier: str) -> str:
    """Start an async Textract job; returns the JobId."""
    location = {"S3Object": {"Bucket": bucket, "Name": key}}
    if tier == "forms":
        return _textract.start_document_analysis(
            DocumentLocation=location, FeatureTypes=_features()
        )["JobId"]
    return _textract.start_document_text_detection(DocumentLocation=location)["JobId"]


def _check_status(tier: str, job_id: str) -> str:
    """One status check for an async job (small, checkpoint-friendly)."""
    if tier == "forms":
        return _textract.get_document_analysis(JobId=job_id, MaxResults=1)["JobStatus"]
    return _textract.get_document_text_detection(JobId=job_id, MaxResults=1)["JobStatus"]


def _get_async(tier: str, job_id: str) -> Dict[str, Any]:
    """Fetch + accumulate all blocks of a completed async job."""
    get_fn = (
        _textract.get_document_analysis
        if tier == "forms"
        else _textract.get_document_text_detection
    )
    blocks: List[Dict[str, Any]] = []
    token = None
    while True:
        resp = get_fn(JobId=job_id, NextToken=token) if token else get_fn(JobId=job_id)
        blocks.extend(resp.get("Blocks", []))
        token = resp.get("NextToken")
        if not token:
            break
    return {"Blocks": blocks}


def _build_output(event: Dict[str, Any], response: Dict[str, Any], tier: str) -> Dict[str, Any]:
    """Parse a Textract response into the compact output passed downstream."""
    bucket, key = _resolve_location(event)
    out: Dict[str, Any] = {
        "doc_id": event.get("doc_id"),
        "source_uri": event.get("source_uri", f"s3://{bucket}/{key}"),
        "content_hash": event.get("content_hash"),
        "bucket": bucket,
        "key": key,
        "extraction_method": METHOD_TEXTRACT,
        "tier": tier,
        # Echo is_image so the downstream NeedsVision? choice can route on it;
        # the task-token result replaces the Step Functions state data.
        "is_image": event.get("is_image", False),
    }
    if tier == "forms":
        document = tx.parse_analyze_forms(response)
        field_count = len(document.all_key_values())
        # Echo the OCR tier's spot-check so a spot-checked document still
        # reaches the AI tier (the task-token result replaces the state data).
        spot_check = bool(event.get("spot_check", False))
        out["spot_check"] = spot_check
        out["spot_check_template_id"] = event.get("spot_check_template_id", "") or ""
        out["document"] = document.to_dict()
        out["table_count"] = tx.count_tables(response)
        out["clear"] = field_count > 0 and not spot_check
        if spot_check:
            out["clarity_reason"] = (
                f"spot-check of trusted template {out['spot_check_template_id']}; "
                "escalating to AI tier for comparison"
            )
        else:
            out["clarity_reason"] = (
                f"structured Forms/Tables recovered {field_count} field(s)"
                if field_count
                else "no fields from structured extraction; escalating to Bedrock"
            )
        return out

    document = tx.parse_detect_text(response)
    out["document"] = document.to_dict()
    template_match = None
    if TEMPLATE_TABLE:
        memory = TemplateMemory(store=DynamoDBTemplateStore(TEMPLATE_TABLE))
        # Only trusted templates may resolve a document on the free path.
        template_match = memory.identify_template(document, trusted_only=True)
    decision = assess_ocr_clarity(document, template_match)
    out["clear"] = decision.is_clear
    out["clarity_reason"] = decision.reason
    out["spot_check"] = False
    out["spot_check_template_id"] = ""
    if decision.is_clear and template_match is not None and select_for_spot_check(
        SPOT_CHECK_PERCENT
    ):
        out["clear"] = False
        out["spot_check"] = True
        out["spot_check_template_id"] = template_match.template_id
        out["clarity_reason"] = (
            f"spot-check of trusted template {template_match.template_id}; "
            "escalating to AI tier for comparison"
        )
    return out


# ----------------------------------------------------------------------
# Durable steps — thin wrappers so each unit of work is checkpointed.
# ----------------------------------------------------------------------
@durable_step
def start_async_step(step_context, bucket: str, key: str, tier: str) -> str:
    return _start_async(bucket, key, tier)


@durable_step
def check_status_step(step_context, tier: str, job_id: str) -> str:
    return _check_status(tier, job_id)


@durable_step
def extract_sync_step(step_context, event, bucket, key, tier) -> Dict[str, Any]:
    return _build_output(event, _run_sync(bucket, key, tier), tier)


@durable_step
def extract_async_step(step_context, event, tier, job_id) -> Dict[str, Any]:
    return _build_output(event, _get_async(tier, job_id), tier)


@durable_step
def send_token_step(step_context, task_token: str, output: Dict[str, Any]) -> bool:
    _sfn.send_task_success(taskToken=task_token, output=json.dumps(output))
    return True


# ----------------------------------------------------------------------
# Durable handler.
# ----------------------------------------------------------------------
@durable_execution
def handler(event: Dict[str, Any], context: DurableContext) -> Dict[str, Any]:
    bucket, key = _resolve_location(event)
    tier = TIER
    task_token = event.get("taskToken")
    logger.info("Durable Textract tier=%s on s3://%s/%s", tier, bucket, key)

    try:
        if _is_async(key):
            job_id = context.step(start_async_step(bucket, key, tier))
            status = "IN_PROGRESS"
            for _ in range(_MAX_POLLS):
                context.wait(Duration.from_seconds(POLL_INTERVAL_SECONDS))
                status = context.step(check_status_step(tier, job_id))
                if status in ("SUCCEEDED", "FAILED"):
                    break
            if status != "SUCCEEDED":
                raise RuntimeError(f"Textract job {job_id} ended with status {status}")
            output = context.step(extract_async_step(event, tier, job_id))
        else:
            output = context.step(extract_sync_step(event, bucket, key, tier))
    except Exception as exc:  # noqa: BLE001 - report failure back to Step Functions
        logger.exception("Durable Textract failed")
        if task_token:
            _sfn.send_task_failure(
                taskToken=task_token, error="TextractError", cause=str(exc)
            )
        raise

    if task_token:
        context.step(send_token_step(task_token, output))
    return output
