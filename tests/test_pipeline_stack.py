# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the Phase 1 sink and pipeline stacks."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.config import AppConfig
from infra.pipeline_stack import PipelineStack
from infra.sink_stack import SinkStack
from infra.storage_stack import StorageStack


def _build(extra_context=None):
    app = cdk.App(context={"idp:appName": "test-idp", **(extra_context or {})})
    config = AppConfig.from_context(app)
    env = cdk.Environment(account="111111111111", region="us-east-1")
    storage = StorageStack(app, "test-idp-storage", config=config, env=env)
    sink = SinkStack(
        app,
        "test-idp-sink",
        config=config,
        env=env,
        table_bucket=storage.table_bucket,
        namespace=storage.namespace,
        namespace_name=storage.namespace_name,
        data_key=storage.data_key,
        access_logs_bucket=storage.access_logs_bucket,
    )
    pipeline = PipelineStack(
        app,
        "test-idp-pipeline",
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
    )
    return {
        "storage": Template.from_stack(storage),
        "sink": Template.from_stack(sink),
        "pipeline": Template.from_stack(pipeline),
    }


@pytest.fixture(scope="module")
def synthesized():
    return _build()


def test_sink_has_iceberg_table_and_firehose(synthesized):
    t = synthesized["sink"]
    t.resource_count_is("AWS::S3Tables::Table", 1)
    t.resource_count_is("AWS::KinesisFirehose::DeliveryStream", 1)
    t.has_resource_properties(
        "AWS::KinesisFirehose::DeliveryStream",
        {
            "IcebergDestinationConfiguration": Match.object_like(
                {
                    "AppendOnly": True,
                    "DestinationTableConfigurationList": [
                        {
                            "DestinationDatabaseName": "idp",
                            "DestinationTableName": "documents",
                        }
                    ],
                }
            )
        },
    )


def test_pipeline_has_expected_lambdas_and_layers(synthesized):
    # preprocess, spatial, sink, textract-ocr, nova-fallback, bda, reconcile.
    # Structured Textract is opt-in (off by default), chart folded into BDA.
    t = synthesized["pipeline"]
    t.resource_count_is("AWS::Lambda::Function", 7)
    t.resource_count_is("AWS::Lambda::LayerVersion", 2)


def test_durable_functions_have_aliases(synthesized):
    t = synthesized["pipeline"]
    # Two durable functions (Textract OCR + BDA), each with a version + alias
    # for the qualified-ARN invocation durable functions require.
    t.resource_count_is("AWS::Lambda::Alias", 2)
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"DurableConfig": {"ExecutionTimeout": 3600, "RetentionPeriodInDays": 7}},
    )


def test_pipeline_creates_bda_project_and_per_type_blueprints(synthesized):
    t = synthesized["pipeline"]
    t.resource_count_is("AWS::Bedrock::DataAutomationProject", 1)
    # One blueprint per document type in the construction pack (BDA matches each
    # document to the best-fitting blueprint).
    t.resource_count_is("AWS::Bedrock::Blueprint", 7)


def test_pipeline_grants_bda_permissions(synthesized):
    synthesized["pipeline"].has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Action": "bedrock:InvokeDataAutomationAsync"}
                        )
                    ]
                )
            }
        },
    )


def test_pipeline_grants_bedrock_permissions(synthesized):
    synthesized["pipeline"].has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Action": Match.array_with(["bedrock:InvokeModel"])}
                        )
                    ]
                )
            }
        },
    )


def test_pipeline_grants_textract_permissions(synthesized):
    synthesized["pipeline"].has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {"Action": Match.array_with(["textract:AnalyzeDocument"])}
                        )
                    ]
                )
            }
        },
    )


def test_pipeline_has_state_machine_and_eventbridge_trigger(synthesized):
    t = synthesized["pipeline"]
    t.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    t.has_resource_properties(
        "AWS::Events::Rule",
        {"EventPattern": Match.object_like({"detail-type": ["Object Created"]})},
    )


def test_preprocess_has_dedup_table_env(synthesized):
    t = synthesized["pipeline"]
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"DEDUP_TABLE": Match.any_value()})}},
    )


def _assert_all_log_groups_encrypted(template):
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert groups, "expected explicit log groups"
    for logical_id, res in groups.items():
        assert "KmsKeyId" in res["Properties"], f"{logical_id} is not KMS-encrypted"


def test_pipeline_and_storage_log_groups_are_kms_encrypted(synthesized):
    # Threat T8: all first-party log groups use the customer-managed data key.
    _assert_all_log_groups_encrypted(synthesized["pipeline"])
    _assert_all_log_groups_encrypted(synthesized["storage"])


def test_state_machine_logs_all_levels_without_execution_data(synthesized):
    # Threat T8: keep LogLevel.ALL (AwsSolutions-SF1) but drop field values
    # and task tokens from the logs.
    synthesized["pipeline"].has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {
            "LoggingConfiguration": Match.object_like(
                {"Level": "ALL", "IncludeExecutionData": False}
            )
        },
    )


def test_preprocess_has_size_and_page_limits(synthesized):
    synthesized["pipeline"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "MAX_FILE_BYTES": str(50 * 1024 * 1024),
                        "MAX_PAGES": "200",
                        "DEDUP_LEASE_SECONDS": "2100",
                    }
                )
            }
        },
    )


def test_sink_marks_dedup_completion(synthesized):
    synthesized["pipeline"].has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "DELIVERY_STREAM_NAME": Match.any_value(),
                        "DEDUP_TABLE": Match.any_value(),
                    }
                )
            }
        },
    )


def _reserved(template):
    return [
        res["Properties"]["ReservedConcurrentExecutions"]
        for res in template.find_resources("AWS::Lambda::Function").values()
        if "ReservedConcurrentExecutions" in res["Properties"]
    ]


def test_paid_tiers_have_reserved_concurrency(synthesized):
    # Threat T5: Nova + BDA are capped at idp:paidTierMaxConcurrency (5).
    assert _reserved(synthesized["pipeline"]) == [5, 5]


def test_paid_tier_concurrency_cap_can_be_disabled():
    templates = _build({"idp:paidTierMaxConcurrency": 0})
    assert _reserved(templates["pipeline"]) == []


def test_state_machine_has_failure_release_and_limit_states(synthesized):
    import json

    machines = synthesized["pipeline"].find_resources("AWS::StepFunctions::StateMachine")
    (machine,) = machines.values()
    definition = json.dumps(machine["Properties"]["DefinitionString"])
    for marker in (
        "ReleaseDedupClaim",
        "ProcessingFailed",
        "RejectedOverLimit",
        "Lambda.TooManyRequestsException",
        "release_dedup",
    ):
        assert marker in definition, marker


def test_storage_key_policy_allows_logs_and_budgets(synthesized):
    synthesized["storage"].has_resource_properties(
        "AWS::KMS::Key",
        {
            "KeyPolicy": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Sid": "CloudWatchLogsUseOfKey",
                                "Principal": {"Service": "logs.us-east-1.amazonaws.com"},
                                "Condition": {
                                    "ArnLike": {
                                        "kms:EncryptionContext:aws:logs:arn": Match.any_value()
                                    }
                                },
                            }
                        ),
                        Match.object_like(
                            {
                                "Sid": "BudgetsPublishToEncryptedTopic",
                                "Principal": {"Service": "budgets.amazonaws.com"},
                            }
                        ),
                    ]
                )
            }
        },
    )


def test_no_bedrock_guardrail_by_default(synthesized):
    t = synthesized["pipeline"]
    t.resource_count_is("AWS::Bedrock::Guardrail", 0)
    t.resource_count_is("AWS::Bedrock::GuardrailVersion", 0)


def test_bedrock_guardrail_opt_in_scopes_apply_guardrail():
    # Threat T1: opt-in prompt-attack guardrail on the Nova fallback.
    t = _build({"idp:bedrockGuardrailEnabled": True})["pipeline"]
    t.resource_count_is("AWS::Bedrock::Guardrail", 1)
    t.resource_count_is("AWS::Bedrock::GuardrailVersion", 1)
    t.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "ContentPolicyConfig": {
                "FiltersConfig": [
                    {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}
                ]
            }
        },
    )
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like(
                    {"GUARDRAIL_ID": Match.any_value(), "GUARDRAIL_VERSION": Match.any_value()}
                )
            }
        },
    )
    statements = [
        stmt
        for res in t.find_resources("AWS::IAM::Policy").values()
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]
        if "bedrock:ApplyGuardrail" in (
            stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        )
    ]
    assert statements
    for stmt in statements:
        resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
        assert "*" not in resources
        # Scoped to the guardrail's own ARN.
        assert all(r.get("Fn::GetAtt", [None, None])[1] == "GuardrailArn" for r in resources)


def test_packs_table_has_stream(synthesized):
    synthesized["storage"].has_resource_properties(
        "AWS::DynamoDB::Table",
        {"StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"}},
    )


def _function_env(template, tier=None, has_key=None):
    """Environment variable dicts of Lambda functions, optionally filtered."""
    envs = []
    for res in template.find_resources("AWS::Lambda::Function").values():
        env = res["Properties"].get("Environment", {}).get("Variables", {})
        if tier is not None and env.get("TIER") != tier:
            continue
        if has_key is not None and has_key not in env:
            continue
        envs.append(env)
    return envs


def test_template_trust_env_wiring(synthesized):
    t = synthesized["pipeline"]
    (ocr_env,) = _function_env(t, tier="ocr")
    assert ocr_env["SPOT_CHECK_PERCENT"] == "5"
    # Nova (BEDROCK_MODEL_ID + TEMPLATE_TABLE) and BDA (BDA_PROJECT_ARN) both
    # get the confirmation count; nothing else does.
    confirm = _function_env(t, has_key="TEMPLATE_CONFIRMATIONS")
    assert len(confirm) == 2
    assert all(env["TEMPLATE_CONFIRMATIONS"] == "2" for env in confirm)
    assert any("BDA_PROJECT_ARN" in env for env in confirm)
    assert any("BEDROCK_MODEL_ID" in env for env in confirm)
    assert len(_function_env(t, has_key="SPOT_CHECK_PERCENT")) == 1


def test_state_machine_passes_spot_check_to_ai_tiers(synthesized):
    import json

    machines = synthesized["pipeline"].find_resources("AWS::StepFunctions::StateMachine")
    (machine,) = machines.values()
    definition = json.dumps(machine["Properties"]["DefinitionString"])
    assert "spot_check_template_id" in definition


def test_structured_textract_passes_spot_check_on_forms_route():
    import json

    t = _build({"idp:structuredTextractEnabled": True})["pipeline"]
    (machine,) = t.find_resources("AWS::StepFunctions::StateMachine").values()
    definition = json.dumps(machine["Properties"]["DefinitionString"])
    # BDA and TextractStructured both forward the spot-check keys.
    assert definition.count("spot_check_template_id.$") == 2
