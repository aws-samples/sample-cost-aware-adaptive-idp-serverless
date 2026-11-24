# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the Phase 0 storage stack.

These run without an AWS account: they synthesize the stack to a
CloudFormation template and assert on its contents.
"""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.config import AppConfig
from infra.storage_stack import StorageStack


def _template(destroy: bool = False) -> Template:
    app = cdk.App(
        context={
            "idp:appName": "test-idp",
            "idp:destroyOnDelete": destroy,
        }
    )
    config = AppConfig.from_context(app)
    stack = StorageStack(app, "test-idp-storage", config=config)
    return Template.from_stack(stack)


def test_creates_expected_resource_counts():
    t = _template()
    t.resource_count_is("AWS::KMS::Key", 1)
    # Shared access-logs bucket + input bucket + BDA output scratch bucket.
    t.resource_count_is("AWS::S3::Bucket", 3)
    # Template store, dedup/cache, and the industry-packs config table.
    t.resource_count_is("AWS::DynamoDB::Table", 3)
    t.resource_count_is("AWS::S3Tables::TableBucket", 1)
    t.resource_count_is("AWS::S3Tables::Namespace", 1)


def test_packs_table_present_and_seeded():
    t = _template()
    # The packs table is keyed by pack_name...
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"KeySchema": [{"AttributeName": "pack_name", "KeyType": "HASH"}]},
    )
    # ...and a custom resource seeds it from the bundled packs on deploy.
    t.resource_count_is("AWS::CloudFormation::CustomResource", 1)


def test_input_bucket_is_encrypted_and_locked_down():
    t = _template()
    t.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {
                        "ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"},
                        "BucketKeyEnabled": True,
                    }
                ]
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "VersioningConfiguration": {"Status": "Enabled"},
        },
    )


def test_input_bucket_glacier_lifecycle_by_default():
    # glacierEnabled defaults to True: transition to Glacier Instant Retrieval
    # at glacierTransitionDays (default 30) and retain (no expiry).
    t = _template()
    t.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Status": "Enabled",
                                "Transitions": Match.array_with(
                                    [
                                        {
                                            "StorageClass": "GLACIER_IR",
                                            "TransitionInDays": 30,
                                        },
                                    ]
                                ),
                            }
                        )
                    ]
                )
            }
        },
    )


def test_source_expiry_when_glacier_disabled():
    # glacierEnabled=False: source docs expire at sourceExpiryDays (30) to cut
    # storage cost, since the structured record has already been forwarded.
    app = cdk.App(
        context={
            "idp:appName": "test-idp",
            "idp:glacierEnabled": False,
        }
    )
    config = AppConfig.from_context(app)
    t = Template.from_stack(StorageStack(app, "test-idp-storage", config=config))
    t.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like(
                            {"Status": "Enabled", "ExpirationInDays": 30}
                        )
                    ]
                )
            }
        },
    )


def test_dynamodb_tables_are_on_demand_and_kms_encrypted():
    t = _template()
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "SSESpecification": {"SSEEnabled": True, "SSEType": "KMS"},
        },
    )


def test_dedup_table_has_ttl():
    t = _template()
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True}},
    )


def test_data_resources_retained_by_default():
    t = _template(destroy=False)
    t.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Retain"})
    t.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})


def test_destroy_flag_sets_delete_policy():
    t = _template(destroy=True)
    t.has_resource("AWS::S3::Bucket", {"DeletionPolicy": "Delete"})
    t.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Delete"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
