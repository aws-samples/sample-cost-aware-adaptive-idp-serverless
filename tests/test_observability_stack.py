# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis tests for the observability + analytics stack."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.config import AppConfig
from infra.observability_stack import ObservabilityStack
from infra.pipeline_stack import PipelineStack
from infra.sink_stack import SinkStack
from infra.storage_stack import StorageStack


def _build(extra_context=None):
    app = cdk.App(context={"idp:appName": "test-idp", **(extra_context or {})})
    config = AppConfig.from_context(app)
    env = cdk.Environment(account="111111111111", region="us-east-1")
    storage = StorageStack(app, "test-idp-storage", config=config, env=env)
    sink = SinkStack(
        app, "test-idp-sink", config=config, env=env,
        table_bucket=storage.table_bucket, namespace=storage.namespace,
        namespace_name=storage.namespace_name, data_key=storage.data_key,
        access_logs_bucket=storage.access_logs_bucket,
    )
    pipeline = PipelineStack(
        app, "test-idp-pipeline", config=config, env=env,
        input_bucket=storage.input_bucket,
        bda_output_bucket=storage.bda_output_bucket,
        dedup_table=storage.dedup_table,
        template_table=storage.template_table,
        packs_table=storage.packs_table, data_key=storage.data_key,
        delivery_stream_name=sink.delivery_stream_name,
        delivery_stream_arn=sink.delivery_stream.attr_arn,
    )
    obs = ObservabilityStack(
        app, "test-idp-observability", config=config, env=env,
        state_machine=pipeline.state_machine,
        functions=pipeline.all_functions,
        delivery_stream_name=sink.delivery_stream_name,
        namespace_name=storage.namespace_name,
        data_key=storage.data_key,
        access_logs_bucket=storage.access_logs_bucket,
        packs_table=storage.packs_table,
    )
    return Template.from_stack(obs)


@pytest.fixture(scope="module")
def observability_template():
    return _build()


def test_has_athena_workgroup_and_named_queries(observability_template):
    observability_template.resource_count_is("AWS::Athena::WorkGroup", 1)
    # cost_savings_summary, method_breakdown, compliance_failures,
    # compliance_needs_review
    observability_template.resource_count_is("AWS::Athena::NamedQuery", 4)


def test_has_dashboard_and_alarms(observability_template):
    observability_template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    # Workflow-failure alarm + Firehose delivery-failure alarm.
    observability_template.resource_count_is("AWS::CloudWatch::Alarm", 2)


def test_alerts_topic_is_kms_encrypted_with_budgets_publish(observability_template):
    t = observability_template
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.has_resource_properties("AWS::SNS::Topic", {"KmsMasterKeyId": Match.any_value()})
    t.has_resource_properties(
        "AWS::SNS::TopicPolicy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": "SNS:Publish",
                                "Principal": {"Service": "budgets.amazonaws.com"},
                            }
                        )
                    ]
                )
            }
        },
    )
    # No subscriber by default: operators subscribe to AlertsTopicArn later.
    t.resource_count_is("AWS::SNS::Subscription", 0)
    t.has_output("AlertsTopicArn", {})


def test_monthly_cost_budget_notifies_topic(observability_template):
    t = observability_template
    t.resource_count_is("AWS::Budgets::Budget", 1)
    (budget,) = t.find_resources("AWS::Budgets::Budget").values()
    props = budget["Properties"]
    assert props["Budget"]["BudgetLimit"] == {"Amount": 100, "Unit": "USD"}
    assert props["Budget"]["TimeUnit"] == "MONTHLY"
    assert props["Budget"]["BudgetType"] == "COST"
    notes = props["NotificationsWithSubscribers"]
    assert [(n["Notification"]["NotificationType"], n["Notification"]["Threshold"]) for n in notes] == [
        ("ACTUAL", 80),
        ("FORECASTED", 100),
    ]
    for n in notes:
        assert n["Subscribers"][0]["SubscriptionType"] == "SNS"


def test_alert_email_adds_subscription():
    t = _build({"idp:alertEmail": "ops@example.com"})
    t.resource_count_is("AWS::SNS::Subscription", 1)
    t.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )


def test_pack_audit_lambda_reads_packs_stream(observability_template):
    t = observability_template
    t.resource_count_is("AWS::Lambda::Function", 1)
    t.resource_count_is("AWS::Lambda::EventSourceMapping", 1)
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": {"ALERT_TOPIC_ARN": Match.any_value()}}},
    )
    groups = t.find_resources("AWS::Logs::LogGroup")
    assert groups
    for res in groups.values():
        assert "KmsKeyId" in res["Properties"]
