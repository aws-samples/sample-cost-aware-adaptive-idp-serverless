#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK app entry point for the cost-aware adaptive IDP sample.

Composes the stacks: durable storage (S3/DynamoDB/S3 Tables/KMS), the
open-table Firehose -> Iceberg sink, the cost-aware processing pipeline
(Textract basic OCR + spatial memory, escalating to a cheap LLM or Bedrock
Data Automation only when needed), Athena cost analytics + a CloudWatch
dashboard, and an optional, opt-in transparent audit-retrieval stack
(S3 Object Lambda; enable with ``-c idp:retrievalEnabled=true``).
"""

import os

import aws_cdk as cdk

from infra.config import AppConfig
from infra.observability_stack import ObservabilityStack
from infra.pipeline_stack import PipelineStack
from infra.retrieval_stack import RetrievalStack
from infra.sink_stack import SinkStack
from infra.storage_stack import StorageStack

app = cdk.App()
config = AppConfig.from_context(app)

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

storage = StorageStack(
    app,
    f"{config.app_name}-storage",
    config=config,
    env=env,
    description="Durable storage foundation (S3, DynamoDB, S3 Tables, KMS) "
    "for the cost-aware adaptive IDP sample.",
)

sink = SinkStack(
    app,
    f"{config.app_name}-sink",
    config=config,
    env=env,
    table_bucket=storage.table_bucket,
    namespace=storage.namespace,
    namespace_name=storage.namespace_name,
    data_key=storage.data_key,
    access_logs_bucket=storage.access_logs_bucket,
    description="Open-table sink: Firehose -> S3 Tables (Apache Iceberg).",
)
sink.add_dependency(storage)

pipeline = PipelineStack(
    app,
    f"{config.app_name}-pipeline",
    config=config,
    env=env,
    input_bucket=storage.input_bucket,
    bda_output_bucket=storage.bda_output_bucket,
    dedup_table=storage.dedup_table,
    template_table=storage.template_table,
    packs_table=storage.packs_table,
    data_key=storage.data_key,
    delivery_stream_name=sink.delivery_stream_name,
    delivery_stream_arn=sink.delivery_stream.attr_arn,
    description="Cost-aware adaptive IDP pipeline (Lambdas + Step Functions "
    "+ EventBridge): Textract OCR + spatial memory, escalating to Nova/BDA.",
)
pipeline.add_dependency(sink)

# Optional audit retrieval (opt-in via idp:retrievalEnabled). S3 Object Lambda
# is available only to existing customers of the service, so it is off by
# default.
retrieval = None
if config.retrieval_enabled:
    retrieval = RetrievalStack(
        app,
        f"{config.app_name}-retrieval",
        config=config,
        env=env,
        input_bucket=storage.input_bucket,
        data_key=storage.data_key,
        description="Transparent audit retrieval (S3 Object Lambda decompress) "
        "for archived source documents.",
    )
    # Retrieval only needs the source bucket + KMS key (both from storage). It
    # builds its own idp_common layer, so it no longer depends on the pipeline
    # stack or its layer export.
    retrieval.add_dependency(storage)

observability = ObservabilityStack(
    app,
    f"{config.app_name}-observability",
    config=config,
    env=env,
    state_machine=pipeline.state_machine,
    functions=pipeline.all_functions,
    delivery_stream_name=sink.delivery_stream_name,
    namespace_name=storage.namespace_name,
    data_key=storage.data_key,
    access_logs_bucket=storage.access_logs_bucket,
    packs_table=storage.packs_table,
    description="Athena cost-savings analytics, CloudWatch dashboard, cost "
    "budget and pack-change alerts.",
)
observability.add_dependency(pipeline)

cdk.Tags.of(app).add("project", config.app_name)

# Opt-in cdk-nag AWS Solutions check. Off by default so a normal `cdk deploy`/
# `cdk synth` does not require the dev-only cdk-nag dependency; enable with
# `-c idp:cdkNag=true` (or CDK_NAG=true) to synth with the checks applied and
# emit per-stack NagReport CSVs in cdk.out. cdk_nag is imported lazily inside
# this block so it is never needed at deploy time.
if app.node.try_get_context("idp:cdkNag") or os.environ.get("CDK_NAG", "").lower() == "true":
    from cdk_nag import AwsSolutionsChecks, NagSuppressions

    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

    # The remaining, deliberately-accepted findings. Resource-scopable grants
    # (bedrock:InvokeDataAutomationAsync, s3-object-lambda:WriteGetObjectResponse)
    # are scoped to specific ARNs in the stacks; Firehose SSE, S3 access logging,
    # and ALL-level Step Functions logging are enabled. What is left is either an
    # action AWS does not make resource-addressable, or a managed policy accepted
    # for this reference sample. Each suppression records that evidence.
    for stack in [s for s in (storage, sink, pipeline, retrieval, observability) if s is not None]:
        NagSuppressions.add_stack_suppressions(
            stack,
            [
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Lambda execution roles use the AWS managed basic- and "
                    "durable-execution policies, and CDK's custom-resource framework "
                    "roles use their own managed policies. Accepted for a reference "
                    "sample; a production deployment should replace these with "
                    "customer-managed least-privilege policies (see README "
                    "'AWS Solutions (cdk-nag) findings').",
                },
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Residual wildcards are either actions AWS does not make "
                    "resource-addressable (textract:DetectDocumentText/AnalyzeDocument "
                    "and their async variants, states:SendTaskSuccess/SendTaskFailure, "
                    "lakeformation:GetDataAccess, bedrock:GetDataAutomationStatus) or "
                    "already resource-scoped grants whose only wildcard is an S3 object "
                    "key, a KMS grant helper, or the model id in bedrock:InvokeModel "
                    "(scoped to foundation-model/inference-profile). The scopable "
                    "grants (InvokeDataAutomationAsync, WriteGetObjectResponse) are "
                    "pinned to specific ARNs. See README 'AWS Solutions (cdk-nag) "
                    "findings'.",
                },
            ],
        )

    # The shared access-logs bucket is the destination for the other four
    # buckets' server access logs; it cannot log to itself without recursing,
    # so it is the one bucket that legitimately has no access logging.
    NagSuppressions.add_resource_suppressions(
        storage.access_logs_bucket,
        [
            {
                "id": "AwsSolutions-S1",
                "reason": "Destination bucket for the pipeline's S3 server access "
                "logs; enabling access logging on the log bucket itself would "
                "recurse. The input, BDA-output, Firehose-error, and Athena-results "
                "buckets deliver their access logs here.",
            }
        ],
    )

app.synth()
