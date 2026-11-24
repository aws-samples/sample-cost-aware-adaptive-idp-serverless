# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Open-table sink stack: Amazon Data Firehose -> S3 Tables (Apache Iceberg).

Creates the ``documents`` Iceberg table (scalar/JSON columns for a clean
Firehose mapping and SQL-friendly cost analytics) and a Firehose delivery
stream that appends merged records to it. Firehose handles buffering and
retries; the sink Lambda only calls ``PutRecord``.

Prerequisite (one-time per account/region, outside CDK): enable S3 Tables
integration with AWS analytics services so the table bucket is federated into
the Glue Data Catalog under ``s3tablescatalog``. See the README.
"""

from typing import List

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3tables as s3tables
from constructs import Construct

from infra.config import AppConfig

# Iceberg table columns. Complex structures (fields, provenance) are stored as
# JSON strings so the Firehose JSON->Iceberg mapping stays flat and reliable,
# while scalar cost columns remain directly queryable in Athena.
_SCHEMA: List[tuple] = [
    ("doc_id", "string", True),
    ("doc_type", "string", False),
    ("source_uri", "string", False),
    ("content_hash", "string", False),
    ("ingested_at", "string", False),
    ("mean_confidence", "double", False),
    ("field_count", "int", False),
    ("free_field_count", "int", False),
    ("paid_field_count", "int", False),
    ("method_summary_json", "string", False),
    ("fields_json", "string", False),
    ("field_provenance_json", "string", False),
    ("compliance_result", "string", False),
    ("compression_algo", "string", False),
]

TABLE_NAME = "documents"


class SinkStack(Stack):
    """Firehose -> S3 Tables Iceberg sink."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        table_bucket: s3tables.CfnTableBucket,
        namespace: s3tables.CfnNamespace,
        namespace_name: str,
        data_key: kms.Key,
        access_logs_bucket: s3.Bucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self._namespace_name = namespace_name
        self._access_logs_bucket = access_logs_bucket

        self.iceberg_table = self._create_iceberg_table(table_bucket, namespace)
        self.error_bucket = self._create_error_bucket(data_key)
        self.firehose_role = self._create_firehose_role(table_bucket, data_key)
        self.delivery_stream = self._create_delivery_stream(table_bucket)

        self._create_outputs()

    @property
    def delivery_stream_name(self) -> str:
        return self.delivery_stream.ref

    # ------------------------------------------------------------------
    def _create_iceberg_table(
        self, table_bucket: s3tables.CfnTableBucket, namespace: s3tables.CfnNamespace
    ) -> s3tables.CfnTable:
        table = s3tables.CfnTable(
            self,
            "DocumentsTable",
            table_name=TABLE_NAME,
            namespace=self._namespace_name,
            table_bucket_arn=table_bucket.attr_table_bucket_arn,
            open_table_format="ICEBERG",
            iceberg_metadata=s3tables.CfnTable.IcebergMetadataProperty(
                iceberg_schema=s3tables.CfnTable.IcebergSchemaProperty(
                    schema_field_list=[
                        s3tables.CfnTable.SchemaFieldProperty(
                            name=name, type=col_type, required=required
                        )
                        for name, col_type, required in _SCHEMA
                    ]
                )
            ),
        )
        table.add_dependency(namespace)
        return table

    def _create_error_bucket(self, data_key: kms.Key) -> s3.Bucket:
        """Holds records Firehose fails to deliver (FailedDataOnly backup).

        Failed records transition to Glacier Instant Retrieval at 30 days (cheap
        but still instantly retrievable for replay) and expire at
        ``error_bucket_expiry_days`` (default 90). The Firehose delivery-failure
        alarm (observability stack) is the signal to replay them before expiry.
        """
        return s3.Bucket(
            self,
            "FirehoseErrorBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            server_access_logs_bucket=self._access_logs_bucket,
            server_access_logs_prefix="firehose-error-bucket/",
            removal_policy=self.config.removal_policy,
            auto_delete_objects=self.config.auto_delete_objects,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="error-records",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(30),
                        )
                    ],
                    expiration=Duration.days(self.config.error_bucket_expiry_days),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )

    def _glue_catalog_arn(self, table_bucket: s3tables.CfnTableBucket) -> str:
        """Federated Glue catalog ARN for the S3 Tables bucket."""
        return self.format_arn(
            service="glue",
            resource="catalog",
            resource_name=f"s3tablescatalog/{table_bucket.table_bucket_name}",
        )

    def _create_firehose_role(
        self, table_bucket: s3tables.CfnTableBucket, data_key: kms.Key
    ) -> iam.Role:
        role = iam.Role(
            self,
            "FirehoseRole",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
            description="Lets Firehose write to the S3 Tables Iceberg table.",
        )

        # S3 Tables data plane + Glue catalog access (via Lake Formation).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:GetNamespace",
                    "s3tables:GetTable",
                    "s3tables:GetTableData",
                    "s3tables:PutTableData",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:UpdateTableMetadataLocation",
                ],
                resources=[
                    table_bucket.attr_table_bucket_arn,
                    f"{table_bucket.attr_table_bucket_arn}/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetCatalog",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetTableVersion",
                    "glue:GetTableVersions",
                    "glue:UpdateTable",
                ],
                # Multi-level federated catalog: glue:GetTable on
                # s3tablescatalog/<bucket>/<ns>/<table> requires permission at
                # every level of the hierarchy — the root catalog, the
                # s3tablescatalog catalog, and its children — plus the database
                # and table. Matches the AWS "Firehose to S3 Tables" IAM policy.
                resources=[
                    self.format_arn(service="glue", resource="catalog", resource_name=None),
                    self.format_arn(
                        service="glue", resource="catalog", resource_name="s3tablescatalog"
                    ),
                    self.format_arn(
                        service="glue", resource="catalog", resource_name="s3tablescatalog/*"
                    ),
                    self.format_arn(
                        service="glue", resource="database", resource_name="s3tablescatalog/*"
                    ),
                    self.format_arn(
                        service="glue", resource="table", resource_name="s3tablescatalog/*"
                    ),
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lakeformation:GetDataAccess"], resources=["*"]
            )
        )
        # Error/backup bucket + KMS.
        self.error_bucket.grant_read_write(role)
        data_key.grant_encrypt_decrypt(role)
        return role

    def _create_delivery_stream(
        self, table_bucket: s3tables.CfnTableBucket
    ) -> firehose.CfnDeliveryStream:
        stream = firehose.CfnDeliveryStream(
            self,
            "DeliveryStream",
            delivery_stream_name=f"{self.config.app_name}-records",
            delivery_stream_type="DirectPut",
            # Server-side encryption for records buffered in the stream
            # (AwsSolutions-KDF1). AWS_OWNED_CMK needs no extra key policy or
            # IAM; records are also SSE-KMS encrypted at rest once written to
            # the S3 error bucket and to S3 Tables.
            delivery_stream_encryption_configuration_input=firehose.CfnDeliveryStream.DeliveryStreamEncryptionConfigurationInputProperty(
                key_type="AWS_OWNED_CMK",
            ),
            iceberg_destination_configuration=firehose.CfnDeliveryStream.IcebergDestinationConfigurationProperty(
                catalog_configuration=firehose.CfnDeliveryStream.CatalogConfigurationProperty(
                    catalog_arn=self._glue_catalog_arn(table_bucket)
                ),
                role_arn=self.firehose_role.role_arn,
                append_only=True,
                s3_configuration=firehose.CfnDeliveryStream.S3DestinationConfigurationProperty(
                    bucket_arn=self.error_bucket.bucket_arn,
                    role_arn=self.firehose_role.role_arn,
                ),
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=64
                ),
                s3_backup_mode="FailedDataOnly",
                destination_table_configuration_list=[
                    firehose.CfnDeliveryStream.DestinationTableConfigurationProperty(
                        destination_database_name=self._namespace_name,
                        destination_table_name=TABLE_NAME,
                    )
                ],
            ),
        )
        stream.add_dependency(self.iceberg_table)
        stream.node.add_dependency(self.firehose_role)
        return stream

    def _create_outputs(self) -> None:
        CfnOutput(
            self,
            "DeliveryStreamName",
            value=self.delivery_stream.ref,
            description="Firehose delivery stream the sink Lambda writes to.",
        )
        CfnOutput(
            self,
            "IcebergTableName",
            value=f"{self._namespace_name}.{TABLE_NAME}",
            description="Namespace-qualified Iceberg table name.",
        )
