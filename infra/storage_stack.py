# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Storage foundation stack.

Provisions the durable, stateful resources that the rest of the pipeline
builds on:

* A customer-managed KMS key for SSE-KMS across data stores.
* The S3 input bucket (source documents) with tiered lifecycle to Glacier.
* DynamoDB tables for the spatial template store and the dedup/cache index.
* An S3 Tables bucket + namespace for the managed Apache Iceberg sink.

These are intentionally isolated in their own stack so they can outlive the
compute/pipeline stacks and be retained on teardown by default.
"""

import json
import sys
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3tables as s3tables
from aws_cdk import custom_resources as cr
from constructs import Construct

from infra.config import AppConfig

_SRC = Path(__file__).resolve().parents[1] / "src"
_RUNTIME = lambda_.Runtime.PYTHON_3_14

# Make the shared engine importable at synth time so we can read the bundled
# packs (single source) to seed the packs table.
_COMMON_LAYER = _SRC / "common_layer" / "python"
if str(_COMMON_LAYER) not in sys.path:
    sys.path.insert(0, str(_COMMON_LAYER))

from idp_common.packs import list_packs, load_pack_data  # noqa: E402

# Inline, dependency-free (boto3 is in the Lambda runtime) custom-resource
# handler that seeds the packs table. It is CREATE-ONLY: each pack is written
# with attribute_not_exists(pack_name), so runtime edits are never overwritten
# by a redeploy. That is what keeps CI/CD deploys drift-free — pack content is
# data in DynamoDB, not a CloudFormation-managed property.
_PACK_SEEDER_CODE = '''
import json
import boto3
from botocore.exceptions import ClientError

_ddb = boto3.client("dynamodb")


def on_event(event, _ctx):
    pid = {"PhysicalResourceId": "pack-seeder"}
    if event.get("RequestType") == "Delete":
        return pid
    props = event.get("ResourceProperties", {})
    table = props["tableName"]
    packs = props.get("packs", {})
    seeded = []
    for name, definition in packs.items():
        try:
            _ddb.put_item(
                TableName=table,
                Item={"pack_name": {"S": name}, "definition": {"S": definition}},
                ConditionExpression="attribute_not_exists(pack_name)",
            )
            seeded.append(name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    pid["Data"] = {"seeded": ",".join(seeded)}
    return pid
'''


class StorageStack(Stack):
    """Durable storage resources for the cost-aware adaptive IDP pattern."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        self.data_key = self._create_kms_key()
        self.access_logs_bucket = self._create_access_logs_bucket()
        self.input_bucket = self._create_input_bucket()
        self.bda_output_bucket = self._create_bda_output_bucket()
        self.template_table = self._create_template_table()
        self.dedup_table = self._create_dedup_table()
        self.packs_table = self._create_packs_table()
        self._seed_packs(self.packs_table)
        self.namespace_name = "idp"
        self.table_bucket, self.namespace = self._create_iceberg_table_bucket()

        self._create_outputs()

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    def _create_kms_key(self) -> kms.Key:
        """Customer-managed key used to encrypt source docs, DynamoDB, the
        solution's CloudWatch log groups and the alerts SNS topic."""
        key = kms.Key(
            self,
            "DataKey",
            description=f"{self.config.app_name} data encryption key (SSE-KMS)",
            enable_key_rotation=True,
            removal_policy=self.config.removal_policy,
            pending_window=Duration.days(7),
        )
        # CloudWatch Logs encrypts log groups associated with this key. The
        # regional logs service principal needs these actions, scoped by the
        # encryption context to log groups in this account and Region (AWS
        # CloudWatch Logs "Encrypt log data using AWS KMS", step 2).
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsUseOfKey",
                principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:{self.partition}:logs:{self.region}:{self.account}"
                            ":log-group:*"
                        )
                    }
                },
            )
        )
        # AWS Budgets publishes cost alerts to the KMS-encrypted alerts topic
        # (observability stack); it needs to generate and decrypt data keys
        # (AWS Budgets "Protecting your Amazon SNS budget alerts data with SSE
        # and AWS KMS"). Scoped to this account.
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="BudgetsPublishToEncryptedTopic",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )
        return key

    def _log_group(self, name: str) -> logs.LogGroup:
        """Retention-bounded log group encrypted with the data key (threat T8)."""
        return logs.LogGroup(
            self,
            name,
            retention=self.config.log_retention,
            removal_policy=self.config.removal_policy,
            encryption_key=self.data_key,
        )

    def _create_access_logs_bucket(self) -> s3.Bucket:
        """Shared destination for S3 server access logs (AwsSolutions-S1).

        The input, BDA-output, Firehose-error, and Athena-results buckets all
        deliver their access logs here under distinct prefixes. It uses SSE-S3
        (not the customer KMS key) so the S3 log-delivery service can write
        without a key-policy grant. A log bucket cannot log to itself without
        recursing, so this is the one bucket that legitimately has no access
        logging; that S1 finding is suppressed with evidence in app.py.
        """
        return s3.Bucket(
            self,
            "AccessLogsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=self.config.removal_policy,
            auto_delete_objects=self.config.auto_delete_objects,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="access-logs-expiry",
                    enabled=True,
                    expiration=Duration.days(90),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )

    def _create_input_bucket(self) -> s3.Bucket:
        """Source-document bucket with configurable archival lifecycle.

        The lifecycle is driven by config so operators can trade durability
        for cost:

        * ``glacier_enabled=True`` (default): objects transition to Glacier
          Instant Retrieval after ``glacier_transition_days`` and are then
          retained indefinitely (long-term archive).
        * ``glacier_enabled=False``: source documents are *expired* after
          ``source_expiry_days`` to minimize storage cost — the pipeline has
          already extracted and forwarded the structured record to the sink,
          so the raw source is disposable.

        Old object versions are always expired to control versioning cost.
        """
        if self.config.glacier_enabled:
            lifecycle_rule = s3.LifecycleRule(
                id="glacier-archival",
                enabled=True,
                transitions=[
                    s3.Transition(
                        storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                        transition_after=self.config.source_lifecycle_transition_days(),
                    ),
                ],
                noncurrent_version_expiration=Duration.days(90),
                abort_incomplete_multipart_upload_after=Duration.days(7),
            )
        else:
            lifecycle_rule = s3.LifecycleRule(
                id="source-expiry",
                enabled=True,
                expiration=self.config.source_lifecycle_expiry(),
                noncurrent_version_expiration=Duration.days(90),
                abort_incomplete_multipart_upload_after=Duration.days(7),
            )

        return s3.Bucket(
            self,
            "InputBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            event_bridge_enabled=True,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="input-bucket/",
            removal_policy=self.config.removal_policy,
            auto_delete_objects=self.config.auto_delete_objects,
            lifecycle_rules=[lifecycle_rule],
        )

    def _create_bda_output_bucket(self) -> s3.Bucket:
        """Scratch bucket for Bedrock Data Automation (BDA) output.

        BDA writes its extraction results here as JSON; the BDA handler reads
        them, builds the structured record, and forwards it to the sink. The
        raw BDA output is transient, so it is expired after
        ``bda_output_expiry_days`` (default 7) to avoid accumulating cost.
        """
        return s3.Bucket(
            self,
            "BdaOutputBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            server_access_logs_bucket=self.access_logs_bucket,
            server_access_logs_prefix="bda-output-bucket/",
            removal_policy=self.config.removal_policy,
            auto_delete_objects=self.config.auto_delete_objects,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="bda-output-expiry",
                    enabled=True,
                    expiration=Duration.days(self.config.bda_output_expiry_days),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
        )

    def _create_template_table(self) -> dynamodb.Table:
        """Spatial template store: one item per learned document template."""
        return dynamodb.Table(
            self,
            "TemplateTable",
            partition_key=dynamodb.Attribute(
                name="template_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.data_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=self.config.removal_policy,
        )

    def _create_dedup_table(self) -> dynamodb.Table:
        """Content-hash dedup/cache index, with TTL on cached entries."""
        return dynamodb.Table(
            self,
            "DedupTable",
            partition_key=dynamodb.Attribute(
                name="content_hash", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.data_key,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=self.config.removal_policy,
        )

    def _create_packs_table(self) -> dynamodb.Table:
        """Industry-pack config store: one item per pack (source of truth at
        runtime). Reconcile and the LLM fallback read packs from here, so field
        and document-type edits take effect with no redeploy. Because the items
        are data (not a CloudFormation property), runtime edits never cause
        stack drift."""
        return dynamodb.Table(
            self,
            "PacksTable",
            partition_key=dynamodb.Attribute(
                name="pack_name", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.data_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=self.config.removal_policy,
            # Change feed for the pack-audit alert Lambda (threat T3): every
            # INSERT/MODIFY/REMOVE of a pack raises an SNS alert with a rule diff.
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
        )

    def _seed_packs(self, table: dynamodb.Table) -> None:
        """Seed the packs table from the bundled YAML packs, create-only.

        The bundled packs are read at synth time and passed to a small custom
        resource that writes any that are missing. Existing items (including
        ones edited at runtime) are left untouched, so redeploys are idempotent
        and non-destructive. Adding a new pack YAML to the repo changes the
        seeded set, which re-runs the resource and inserts only the new pack.
        """
        packs = {name: json.dumps(load_pack_data(name)) for name in list_packs()}
        seeder = lambda_.Function(
            self,
            "PackSeederFn",
            runtime=_RUNTIME,
            handler="index.on_event",
            code=lambda_.Code.from_inline(_PACK_SEEDER_CODE),
            timeout=Duration.seconds(60),
            memory_size=256,
            log_group=self._log_group("PackSeederLogs"),
        )
        table.grant_write_data(seeder)
        provider = cr.Provider(
            self,
            "PackSeederProvider",
            on_event_handler=seeder,
            log_group=self._log_group("PackSeederProviderLogs"),
        )
        CustomResource(
            self,
            "PackSeeder",
            service_token=provider.service_token,
            properties={"tableName": table.table_name, "packs": packs},
        )

    def _create_iceberg_table_bucket(self):
        """S3 Tables bucket + namespace for the managed Iceberg sink.

        The Iceberg table itself is created by the sink stack (Phase 1) once
        the record schema is defined; here we only establish the container.
        """
        table_bucket = s3tables.CfnTableBucket(
            self,
            "DocumentsTableBucket",
            table_bucket_name=f"{self.config.app_name}-tables",
        )
        # S3 Tables have no auto-empty equivalent: when destroy_on_delete is
        # set, CloudFormation can only delete the table bucket if its
        # namespaces/tables are already gone. The sink stack owns table
        # deletion; here we align the bucket's removal policy with config and
        # document that a non-empty table bucket must be emptied first (see
        # README teardown notes).
        table_bucket.apply_removal_policy(self.config.removal_policy)
        namespace = s3tables.CfnNamespace(
            self,
            "DocumentsNamespace",
            namespace=self.namespace_name,
            table_bucket_arn=table_bucket.attr_table_bucket_arn,
        )
        namespace.add_dependency(table_bucket)
        namespace.apply_removal_policy(self.config.removal_policy)
        return table_bucket, namespace

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------
    def _create_outputs(self) -> None:
        CfnOutput(
            self,
            "InputBucketName",
            value=self.input_bucket.bucket_name,
            description="S3 bucket where source documents are uploaded.",
        )
        CfnOutput(
            self,
            "BdaOutputBucketName",
            value=self.bda_output_bucket.bucket_name,
            description="Scratch S3 bucket for Bedrock Data Automation output.",
        )
        CfnOutput(
            self,
            "TemplateTableName",
            value=self.template_table.table_name,
            description="DynamoDB table holding learned spatial templates.",
        )
        CfnOutput(
            self,
            "DedupTableName",
            value=self.dedup_table.table_name,
            description="DynamoDB table for content-hash dedup/cache.",
        )
        CfnOutput(
            self,
            "PacksTableName",
            value=self.packs_table.table_name,
            description="DynamoDB table holding industry pack definitions (runtime config).",
        )
        CfnOutput(
            self,
            "TableBucketArn",
            value=self.table_bucket.attr_table_bucket_arn,
            description="S3 Tables bucket ARN for the Iceberg sink.",
        )
        CfnOutput(
            self,
            "DataKeyArn",
            value=self.data_key.key_arn,
            description="KMS key ARN used for SSE-KMS across data stores.",
        )
