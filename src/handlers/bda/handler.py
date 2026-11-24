# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bedrock Data Automation (BDA) durable Lambda — managed escalation tier.

Reached when cheap Textract OCR is unclear *and* the document is image/chart/
visually complex (scanned pages, plots, mixed layouts). BDA is a managed
service: it extracts structured fields with bounding boxes (visual grounding),
so this handler also **learns the layout into spatial memory** — future
documents of the same type then resolve for free on the OCR + spatial path.

Like Textract async, BDA runs asynchronously. This is a Lambda **durable
function**: ``context.wait(...)`` suspends between status checks without
compute charge and resumes with checkpoint/replay recovery. Step Functions
invokes it with the **task-token** pattern and resumes when the handler calls
``SendTaskSuccess``.

The pure parsing/record logic lives in ``idp_common.bda`` and is unit-tested;
``_build_output`` below is the unit-tested seam.

Template poisoning (threat T2): a layout learned here starts provisional and
only becomes trusted after ``TEMPLATE_CONFIRMATIONS`` AI-checked documents
agree on it. When the OCR tier spot-checked a trusted template, the BDA result
is compared with the template path (the OCR document in the event) and the
template is demoted on disagreement. The BDA result is the record either way.
"""

import json
import logging
import os
from typing import Any, Dict, Tuple

import boto3
from aws_durable_execution_sdk_python import (  # type: ignore
    DurableContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import Duration  # type: ignore

from idp_common import bda, grounding
from idp_common.packs import resolve_pack
from idp_common.record import METHOD_BDA, DocumentRecord
from idp_common.models import Document
from idp_common.template_memory import DynamoDBTemplateStore, TemplateMemory, compare

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_bda_runtime = boto3.client("bedrock-data-automation-runtime")
_s3 = boto3.client("s3")
_sfn = boto3.client("stepfunctions")

PROJECT_ARN = os.environ.get("BDA_PROJECT_ARN", "")
OUTPUT_BUCKET = os.environ.get("BDA_OUTPUT_BUCKET", "")
TEMPLATE_TABLE = os.environ.get("TEMPLATE_TABLE", "")
PACK_NAME = os.environ.get("PACK_NAME", "construction")
PACKS_TABLE = os.environ.get("PACKS_TABLE", "")
# AI-checked documents that must agree before a learned template is trusted (T2).
TEMPLATE_CONFIRMATIONS = max(1, int(os.environ.get("TEMPLATE_CONFIRMATIONS", "2")))

# Cache the resolved pack across invocations to map the matched blueprint back
# to a document-type name (and to learn templates with their doc type).
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        try:
            _PACK = resolve_pack(PACK_NAME, table_name=PACKS_TABLE or None)
        except Exception:  # pragma: no cover - degrade gracefully
            logger.exception("Could not resolve pack %s; BDA doc_type mapping disabled", PACK_NAME)
            _PACK = False
    return _PACK or None


def _type_names() -> list:
    pack = _pack()
    return [dt.name for dt in pack.document_types] if pack else []
REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
_MAX_POLLS = int(os.environ.get("MAX_POLLS", "120"))  # durable waits are free
_TERMINAL = {"Success", "ServiceError", "ClientError"}


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


def _output_prefix(doc_id: str) -> str:
    return f"s3://{OUTPUT_BUCKET}/bda/{doc_id}/"


def _start(bucket: str, key: str, doc_id: str) -> str:
    """Start an async BDA job; returns the invocationArn."""
    resp = _bda_runtime.invoke_data_automation_async(
        inputConfiguration=bda.build_input_config(f"s3://{bucket}/{key}"),
        outputConfiguration=bda.build_output_config(_output_prefix(doc_id)),
        dataAutomationConfiguration=bda.build_automation_config(PROJECT_ARN),
        dataAutomationProfileArn=bda.profile_arn(REGION, ACCOUNT_ID),
    )
    return resp["invocationArn"]


def _status(invocation_arn: str) -> Dict[str, Any]:
    return _bda_runtime.get_data_automation_status(invocationArn=invocation_arn)


def _read_json_uri(s3_uri: str) -> Dict[str, Any]:
    _, _, rest = s3_uri.partition("s3://")
    bucket, _, key = rest.partition("/")
    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def _read_result(output_uri: str) -> Dict[str, Any]:
    """Resolve BDA's output.

    ``get_data_automation_status`` points at either the result JSON directly or
    a ``job_metadata`` document listing per-segment output paths. Follow the
    first segment's custom-output path when present.
    """
    payload = _read_json_uri(output_uri)
    segments = payload.get("output_metadata")
    if not segments:
        return payload  # already the result JSON
    first = segments[0] or {}
    seg_out = first.get("segment_metadata") or first.get("custom_output") or []
    if isinstance(seg_out, list) and seg_out:
        path = (seg_out[0] or {}).get("custom_output_path") or (
            seg_out[0] or {}
        ).get("standard_output_path")
        if path:
            return _read_json_uri(path)
    return payload


def _build_output(event: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a BDA result into a sink-ready record and train spatial memory."""
    bucket, key = _resolve_location(event)
    parsed = bda.parse_result(result)
    # With per-type blueprints, map the matched blueprint back to the doc type.
    resolved_type = bda.resolve_doc_type(result, _type_names())
    doc_type = resolved_type or event.get("doc_type", "unknown")

    record = DocumentRecord(
        doc_id=event["doc_id"],
        doc_type=doc_type,
        source_uri=event.get("source_uri", f"s3://{bucket}/{key}"),
        content_hash=event.get("content_hash"),
    )
    for name, value in parsed.fields.items():
        # BDA's own visual grounding: a value is verified only when BDA
        # returned a bounding box for it.
        bbox = _bbox_for(parsed, name)
        record.add_field(
            name=name,
            value=value,
            method=METHOD_BDA,
            confidence=parsed.confidence_for(name),
            region_bbox=bbox,
            grounded=bbox is not None,
        )

    out = record.to_dict()
    out["template_id"] = None
    out["template_state"] = None
    if TEMPLATE_TABLE:
        try:
            memory = TemplateMemory(store=DynamoDBTemplateStore(TEMPLATE_TABLE))
            spot_id = event.get("spot_check_template_id")
            if event.get("spot_check") and spot_id:
                template = memory.get_template(spot_id)
                if template is not None:
                    ocr_doc = Document.from_dict(event.get("document") or {"pages": []})
                    agree, reason = compare(
                        ocr_doc, template.doc_type, resolved_type, out["fields"]
                    )
                    if not agree:
                        memory.demote(spot_id)
                        logger.warning(
                            "Spot-check demoted template %s: %s", spot_id, reason
                        )
                    out["spot_check_result"] = "agree" if agree else "disagree"
            # BDA output carries bounding boxes -> learn the layout (provisional
            # until confirmed) for free replay. Only a document where BDA
            # returned a box for every populated value counts toward trust
            # (T2); empty blueprint fields assert nothing and do not block it,
            # and BDA's generated/catch-all fields (summary, document_date,
            # reference_number, issuing_organization, prepared_by) are left
            # out unless the pack declares them.
            if parsed.document.all_key_values():
                pack = _pack()
                pack_fields = pack.fields_for(resolved_type) if pack else []
                template_id, state = memory.learn_from_ai(
                    parsed.document,
                    doc_type=resolved_type,
                    confirmations_required=TEMPLATE_CONFIRMATIONS,
                    verified=grounding.all_verified(
                        bda.trust_provenance(out["field_provenance"], pack_fields),
                        fields=out["fields"],
                    ),
                )
                out["template_id"] = template_id
                out["template_state"] = state
        except Exception:  # pragma: no cover - learning is best-effort
            logger.exception("Spatial learning from BDA output failed")

    out["extraction_method"] = METHOD_BDA
    return out


def _bbox_for(parsed: bda.ParsedBda, name: str):
    for kv in parsed.document.all_key_values():
        if kv.key_text == name:
            return kv.bbox.to_dict()
    return None


# ----------------------------------------------------------------------
# Durable steps.
# ----------------------------------------------------------------------
@durable_step
def start_step(step_context, bucket: str, key: str, doc_id: str) -> str:
    return _start(bucket, key, doc_id)


@durable_step
def status_step(step_context, invocation_arn: str) -> Dict[str, Any]:
    return _status(invocation_arn)


@durable_step
def extract_step(step_context, event, output_uri) -> Dict[str, Any]:
    return _build_output(event, _read_result(output_uri))


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
    doc_id = event["doc_id"]
    task_token = event.get("taskToken")
    logger.info("Durable BDA on s3://%s/%s", bucket, key)

    try:
        invocation_arn = context.step(start_step(bucket, key, doc_id))
        status: Dict[str, Any] = {"status": "InProgress"}
        for _ in range(_MAX_POLLS):
            context.wait(Duration.from_seconds(POLL_INTERVAL_SECONDS))
            status = context.step(status_step(invocation_arn))
            if status.get("status") in _TERMINAL:
                break
        state = status.get("status")
        if state != "Success":
            raise RuntimeError(
                f"BDA job ended with status {state}: "
                f"{status.get('errorMessage', 'no detail')}"
            )
        output_uri = status["outputConfiguration"]["s3Uri"]
        output = context.step(extract_step(event, output_uri))
    except Exception as exc:  # noqa: BLE001 - report failure back to Step Functions
        logger.exception("Durable BDA failed")
        if task_token:
            _sfn.send_task_failure(taskToken=task_token, error="BdaError", cause=str(exc))
        raise

    if task_token:
        context.step(send_token_step(task_token, output))
    return output
