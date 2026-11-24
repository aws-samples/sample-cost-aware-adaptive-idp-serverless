# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Retrieval stack: transparent audit retrieval of archived source documents.

Composes three pieces around the source bucket:

* a supporting S3 Access Point on the input bucket,
* an S3 Object Lambda Access Point that routes GETs through a transform, and
* the retrieval Lambda that decompresses on the fly.

Auditors GET through the Object Lambda Access Point and always receive the
original document, regardless of the archival compression used.
"""

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3objectlambda as s3ol
from constructs import Construct

from infra.config import AppConfig

_SRC = Path(__file__).resolve().parents[1] / "src"
_RUNTIME = lambda_.Runtime.PYTHON_3_14


class RetrievalStack(Stack):
    """Object Lambda Access Point for transparent decompression on GET."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        input_bucket: s3.Bucket,
        data_key: kms.Key,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        # Build the shared idp_common layer from source here rather than
        # importing the pipeline stack's layer ARN. A cross-stack layer export
        # breaks every time the layer content changes (CloudFormation refuses to
        # update an export that another stack imports), so each stack owns its
        # copy. CDK dedupes identical assets in the bootstrap bucket.
        common_layer = lambda_.LayerVersion(
            self,
            "CommonLayer",
            code=lambda_.Code.from_asset(
                str(_SRC / "common_layer"),
                exclude=["**/__pycache__", "**/*.pyc"],
            ),
            compatible_runtimes=[_RUNTIME],
            description="Shared idp_common library (retrieval handler).",
        )

        self.retrieval_fn = lambda_.Function(
            self,
            "RetrievalFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "retrieval")),
            layers=[common_layer],
            timeout=Duration.seconds(60),
            memory_size=512,
            # Retention-bounded, encrypted with the data key (threat T8).
            log_group=logs.LogGroup(
                self,
                "RetrievalLogs",
                retention=config.log_retention,
                removal_policy=config.removal_policy,
                encryption_key=data_key,
            ),
        )
        # Object Lambda must be able to return the transformed object. Scope
        # WriteGetObjectResponse to this stack's Object Lambda Access Point ARN
        # rather than "*" (the action supports the objectlambdaaccesspoint
        # resource type). The ARN is built by name to avoid a circular
        # dependency, since the access point references this function.
        object_lambda_ap_arn = self.format_arn(
            service="s3-object-lambda",
            resource="accesspoint",
            resource_name=f"{config.app_name}-retrieve-olap",
        )
        self.retrieval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3-object-lambda:WriteGetObjectResponse"],
                resources=[object_lambda_ap_arn],
            )
        )
        input_bucket.grant_read(self.retrieval_fn)
        data_key.grant_decrypt(self.retrieval_fn)

        supporting_ap = s3.CfnAccessPoint(
            self,
            "SupportingAccessPoint",
            bucket=input_bucket.bucket_name,
            name=f"{config.app_name}-source-ap",
        )

        self.object_lambda_ap = s3ol.CfnAccessPoint(
            self,
            "RetrievalObjectLambdaAP",
            name=f"{config.app_name}-retrieve-olap",
            object_lambda_configuration=s3ol.CfnAccessPoint.ObjectLambdaConfigurationProperty(
                supporting_access_point=supporting_ap.attr_arn,
                transformation_configurations=[
                    s3ol.CfnAccessPoint.TransformationConfigurationProperty(
                        actions=["GetObject"],
                        content_transformation={
                            "AwsLambda": {
                                "FunctionArn": self.retrieval_fn.function_arn
                            }
                        },
                    )
                ],
                cloud_watch_metrics_enabled=True,
            ),
        )
        self.object_lambda_ap.add_dependency(supporting_ap)

        CfnOutput(
            self,
            "ObjectLambdaAccessPointArn",
            value=self.object_lambda_ap.attr_arn,
            description="GET through this Object Lambda AP for transparent decompress.",
        )