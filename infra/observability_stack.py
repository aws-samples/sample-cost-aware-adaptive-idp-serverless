# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Observability + analytics stack.

Two concerns, both about making the pattern measurable:

* Analytics: an Athena workgroup (with its own results bucket) plus saved
  cost-savings queries over the S3 Tables ``documents`` table, so the savings
  are provable in SQL.
* Observability: a CloudWatch dashboard for the state machine, the Lambdas,
  and the Firehose sink, plus an alarm on workflow failures.
* Alerts: a KMS-encrypted SNS topic that receives AWS Budgets cost alerts
  (monthly budget) and pack-change alerts from the PacksTable stream.
"""

from pathlib import Path
from typing import List

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_athena as athena
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_events
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from infra.config import AppConfig

_QUERIES_DIR = Path(__file__).resolve().parents[1] / "analytics" / "queries"
_SRC = Path(__file__).resolve().parents[1] / "src"
_RUNTIME = lambda_.Runtime.PYTHON_3_14


class ObservabilityStack(Stack):
    """Athena analytics + CloudWatch dashboard for the pipeline."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        state_machine: sfn.IStateMachine,
        functions: List[lambda_.IFunction],
        delivery_stream_name: str,
        namespace_name: str,
        data_key: kms.Key,
        access_logs_bucket: s3.Bucket,
        packs_table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self._access_logs_bucket = access_logs_bucket

        self.results_bucket = self._create_results_bucket(data_key)
        self.workgroup = self._create_workgroup()
        self._create_named_queries(namespace_name)
        self._create_dashboard(state_machine, functions, delivery_stream_name)
        self._create_failure_alarm(state_machine)
        self._create_firehose_alarm(delivery_stream_name)
        self.alert_topic = self._create_alert_topic(data_key)
        self.budget = self._create_budget(self.alert_topic)
        self.pack_audit_fn = self._create_pack_audit(
            packs_table, self.alert_topic, data_key
        )

        CfnOutput(
            self,
            "AthenaWorkGroupName",
            value=self.workgroup.name,
            description="Athena workgroup for cost-savings queries.",
        )
        CfnOutput(
            self,
            "AlertsTopicArn",
            value=self.alert_topic.topic_arn,
            description="SNS topic for budget and pack-change alerts; "
            "subscribe to receive them.",
        )
        CfnOutput(
            self,
            "MonthlyBudgetName",
            value=f"{self.config.app_name}-monthly-cost",
            description="AWS Budgets monthly cost budget for this account.",
        )

    # ------------------------------------------------------------------
    # Alerts: SNS topic, cost budget, pack-change audit (threats T5, T3)
    # ------------------------------------------------------------------
    def _create_alert_topic(self, data_key: kms.Key) -> sns.Topic:
        """KMS-encrypted alerts topic: AWS Budgets and the pack-audit Lambda
        publish here. With no ``idp:alertEmail`` the topic has no subscribers;
        subscribe to the ``AlertsTopicArn`` output later."""
        topic = sns.Topic(
            self,
            "AlertsTopic",
            topic_name=f"{self.config.app_name}-alerts",
            master_key=data_key,
            enforce_ssl=True,
        )
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowBudgetsPublish",
                principals=[iam.ServicePrincipal("budgets.amazonaws.com")],
                actions=["SNS:Publish"],
                resources=[topic.topic_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{self.partition}:budgets::{self.account}:*"
                    },
                },
            )
        )
        if self.config.alert_email:
            topic.add_subscription(sns_subs.EmailSubscription(self.config.alert_email))
        return topic

    def _create_budget(self, topic: sns.Topic) -> budgets.CfnBudget:
        """Account-wide monthly COST budget alerting at 80% actual and 100%
        forecast spend (``idp:monthlyBudgetUsd``)."""

        def _notify(notification_type: str, threshold: float):
            return budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    comparison_operator="GREATER_THAN",
                    notification_type=notification_type,
                    threshold=threshold,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[
                    budgets.CfnBudget.SubscriberProperty(
                        address=topic.topic_arn, subscription_type="SNS"
                    )
                ],
            )

        budget = budgets.CfnBudget(
            self,
            "MonthlyCostBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{self.config.app_name}-monthly-cost",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=self.config.monthly_budget_usd, unit="USD"
                ),
            ),
            notifications_with_subscribers=[
                _notify("ACTUAL", 80),
                _notify("FORECASTED", 100),
            ],
        )
        # The topic policy must allow Budgets to publish before the budget
        # validates its SNS subscriber.
        budget.node.add_dependency(topic)
        return budget

    def _create_pack_audit(
        self, packs_table: dynamodb.ITable, topic: sns.Topic, data_key: kms.Key
    ) -> lambda_.Function:
        """PacksTable stream -> Lambda -> SNS alert with the rule diff."""
        # Own copy of the common layer (a cross-stack layer export breaks
        # whenever the layer content changes; see retrieval_stack.py).
        common_layer = lambda_.LayerVersion(
            self,
            "CommonLayer",
            code=lambda_.Code.from_asset(
                str(_SRC / "common_layer"),
                exclude=["**/__pycache__", "**/*.pyc"],
            ),
            compatible_runtimes=[_RUNTIME],
            description="Shared idp_common library (pack-audit handler).",
        )
        fn = lambda_.Function(
            self,
            "PackAuditFn",
            runtime=_RUNTIME,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_SRC / "handlers" / "pack_audit")),
            layers=[common_layer],
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"ALERT_TOPIC_ARN": topic.topic_arn},
            log_group=logs.LogGroup(
                self,
                "PackAuditLogs",
                retention=self.config.log_retention,
                removal_policy=self.config.removal_policy,
                encryption_key=data_key,
            ),
        )
        fn.add_event_source(
            lambda_events.DynamoEventSource(
                packs_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=3,
                bisect_batch_on_error=True,
            )
        )
        topic.grant_publish(fn)
        return fn

    # ------------------------------------------------------------------
    def _create_results_bucket(self, data_key: kms.Key) -> s3.Bucket:
        return s3.Bucket(
            self,
            "AthenaResultsBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            server_access_logs_bucket=self._access_logs_bucket,
            server_access_logs_prefix="athena-results-bucket/",
            removal_policy=self.config.removal_policy,
            auto_delete_objects=self.config.auto_delete_objects,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(self.config.athena_results_expiry_days),
                    enabled=True,
                )
            ],
        )

    def _create_workgroup(self) -> athena.CfnWorkGroup:
        return athena.CfnWorkGroup(
            self,
            "AnalyticsWorkGroup",
            name=f"{self.config.app_name}-analytics",
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{self.results_bucket.bucket_name}/results/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3"
                    ),
                ),
            ),
        )

    def _create_named_queries(self, namespace_name: str) -> None:
        for sql_path in sorted(_QUERIES_DIR.glob("*.sql")):
            named_query = athena.CfnNamedQuery(
                self,
                f"Query{_pascal(sql_path.stem)}",
                database=namespace_name,
                query_string=sql_path.read_text(encoding="utf-8"),
                name=f"{self.config.app_name}-{sql_path.stem}",
                description=f"Cost-aware IDP analytics: {sql_path.stem}",
                work_group=self.workgroup.name,
            )
            # ``work_group`` is a plain string, so CloudFormation cannot infer
            # that the query must be created after the workgroup. Declare the
            # dependency explicitly, otherwise the queries race ahead and fail
            # with "WorkGroup is not found".
            named_query.add_dependency(self.workgroup)

    def _create_dashboard(
        self,
        state_machine: sfn.IStateMachine,
        functions: List[lambda_.IFunction],
        delivery_stream_name: str,
    ) -> None:
        dashboard = cw.Dashboard(
            self,
            "PipelineDashboard",
            dashboard_name=f"{self.config.app_name}-pipeline",
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Workflow executions",
                left=[
                    state_machine.metric_started(),
                    state_machine.metric_succeeded(),
                    state_machine.metric_failed(),
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Lambda invocations",
                left=[fn.metric_invocations() for fn in functions],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Lambda errors",
                left=[fn.metric_errors() for fn in functions],
                width=12,
            ),
            cw.GraphWidget(
                title="Firehose records delivered to Iceberg",
                left=[
                    cw.Metric(
                        namespace="AWS/Firehose",
                        metric_name="DeliveryToIceberg.Records",
                        dimensions_map={"DeliveryStreamName": delivery_stream_name},
                        statistic="Sum",
                    )
                ],
                width=12,
            ),
        )

    def _create_failure_alarm(self, state_machine: sfn.IStateMachine) -> None:
        state_machine.metric_failed().create_alarm(
            self,
            "WorkflowFailuresAlarm",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="One or more IDP workflow executions failed.",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )

    def _create_firehose_alarm(self, delivery_stream_name: str) -> None:
        """Alarm on Firehose delivery success dropping below 100%.

        Failed records land in the error/backup bucket; that bucket keeps them
        for ``error_bucket_expiry_days`` so they can be replayed. This alarm is
        the signal to replay them before they expire.
        """
        success_ratio = cw.Metric(
            namespace="AWS/Firehose",
            metric_name="DeliveryToIceberg.Success",
            dimensions_map={"DeliveryStreamName": delivery_stream_name},
            statistic="Average",
            period=Duration.minutes(5),
        )
        success_ratio.create_alarm(
            self,
            "FirehoseDeliveryFailureAlarm",
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            alarm_description=(
                "Firehose failed to deliver records to Iceberg; replay from the "
                "error bucket before they expire."
            ),
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )


def _pascal(stem: str) -> str:
    return "".join(part.capitalize() for part in stem.replace("-", "_").split("_"))
