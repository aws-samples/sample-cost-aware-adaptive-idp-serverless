# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the retrieval (Object Lambda) stack."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.config import AppConfig
from infra.retrieval_stack import RetrievalStack
from infra.storage_stack import StorageStack


@pytest.fixture(scope="module")
def retrieval_template():
    app = cdk.App(context={"idp:appName": "test-idp"})
    config = AppConfig.from_context(app)
    env = cdk.Environment(account="111111111111", region="us-east-1")
    storage = StorageStack(app, "test-idp-storage", config=config, env=env)
    retrieval = RetrievalStack(
        app, "test-idp-retrieval", config=config, env=env,
        input_bucket=storage.input_bucket,
        data_key=storage.data_key,
    )
    return Template.from_stack(retrieval)


def test_has_object_lambda_access_point(retrieval_template):
    retrieval_template.resource_count_is("AWS::S3::AccessPoint", 1)
    retrieval_template.resource_count_is("AWS::S3ObjectLambda::AccessPoint", 1)
    retrieval_template.resource_count_is("AWS::Lambda::Function", 1)


def test_grants_write_get_object_response(retrieval_template):
    retrieval_template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Action": "s3-object-lambda:WriteGetObjectResponse"}
                        )
                    ]
                )
            }
        },
    )


def test_retrieval_log_group_is_kms_encrypted(retrieval_template):
    retrieval_template.resource_count_is("AWS::Logs::LogGroup", 1)
    retrieval_template.has_resource_properties(
        "AWS::Logs::LogGroup", {"KmsKeyId": Match.any_value()}
    )
