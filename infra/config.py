# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared application configuration resolved from CDK context.

Centralizes naming, feature toggles, lifecycle/retention windows, and
removal-policy decisions so every stack behaves consistently. Because this is
an open-source sample, as much as possible is configurable via ``cdk.json``
context (or ``-c key=value`` overrides), with safe defaults.
"""

from dataclasses import dataclass

from aws_cdk import Duration, RemovalPolicy
from constructs import Construct


@dataclass(frozen=True)
class AppConfig:
    """Resolved configuration for the IDP application."""

    # Naming / teardown
    app_name: str
    destroy_on_delete: bool

    # Feature toggles
    annotations_enabled: bool
    structured_textract_enabled: bool
    # Optional audit-retrieval stack (S3 Object Lambda access point). Off by
    # default: S3 Object Lambda is closed to new AWS customers since
    # 7 November 2025.
    retrieval_enabled: bool

    # Models / packs
    bedrock_model_id: str
    pack_name: str

    # Observability
    log_retention_days: int

    # Lifecycle / archival (all configurable)
    glacier_enabled: bool
    glacier_transition_days: int
    source_expiry_days: int
    error_bucket_expiry_days: int
    bda_output_expiry_days: int
    athena_results_expiry_days: int

    # Cost guardrails (threat T5)
    max_file_size_mb: int
    max_pages: int
    paid_tier_max_concurrency: int
    monthly_budget_usd: int
    alert_email: str

    # Prompt-injection hardening (threat T1): optional Bedrock Guardrail
    # prompt-attack filter on the Nova fallback (default off).
    bedrock_guardrail_enabled: bool

    # Template-poisoning hardening (threat T2): AI-checked documents needed
    # before a learned template is trusted, and the share (0-100 %) of
    # trusted-template documents spot-checked by the AI tier.
    template_confirmations: int
    spot_check_percent: int

    @staticmethod
    def from_context(scope: Construct) -> "AppConfig":
        """Build an AppConfig from CDK context values on ``scope``."""
        ctx = scope.node.try_get_context
        return AppConfig(
            app_name=str(ctx("idp:appName") or "cost-aware-idp"),
            destroy_on_delete=_as_bool(ctx("idp:destroyOnDelete"), default=False),
            annotations_enabled=_as_bool(ctx("idp:annotationsEnabled"), default=False),
            structured_textract_enabled=_as_bool(
                ctx("idp:structuredTextractEnabled"), default=False
            ),
            retrieval_enabled=_as_bool(ctx("idp:retrievalEnabled"), default=False),
            bedrock_model_id=str(
                ctx("idp:bedrockModelId") or "us.amazon.nova-lite-v1:0"
            ),
            pack_name=str(ctx("idp:packName") or "construction"),
            log_retention_days=_as_int(ctx("idp:logRetentionDays"), default=30),
            glacier_enabled=_as_bool(ctx("idp:glacierEnabled"), default=True),
            glacier_transition_days=_as_int(
                ctx("idp:glacierTransitionDays"), default=30
            ),
            source_expiry_days=_as_int(ctx("idp:sourceExpiryDays"), default=30),
            error_bucket_expiry_days=_as_int(
                ctx("idp:errorBucketExpiryDays"), default=90
            ),
            bda_output_expiry_days=_as_int(ctx("idp:bdaOutputExpiryDays"), default=7),
            athena_results_expiry_days=_as_int(
                ctx("idp:athenaResultsExpiryDays"), default=30
            ),
            max_file_size_mb=_as_int(ctx("idp:maxFileSizeMb"), default=50),
            max_pages=_as_int(ctx("idp:maxPages"), default=200),
            paid_tier_max_concurrency=max(
                0, _as_int(ctx("idp:paidTierMaxConcurrency"), default=5)
            ),
            monthly_budget_usd=_as_int(ctx("idp:monthlyBudgetUsd"), default=100),
            alert_email=str(ctx("idp:alertEmail") or "").strip(),
            bedrock_guardrail_enabled=_as_bool(
                ctx("idp:bedrockGuardrailEnabled"), default=False
            ),
            template_confirmations=max(
                1, _as_int(ctx("idp:templateConfirmations"), default=2)
            ),
            spot_check_percent=min(
                100, max(0, _as_int(ctx("idp:spotCheckPercent"), default=5))
            ),
        )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def removal_policy(self) -> RemovalPolicy:
        """RemovalPolicy for data resources based on ``destroy_on_delete``."""
        return RemovalPolicy.DESTROY if self.destroy_on_delete else RemovalPolicy.RETAIN

    @property
    def auto_delete_objects(self) -> bool:
        """Auto-empty S3 buckets on removal (avoids the 'bucket not empty' trap)."""
        return self.destroy_on_delete

    @property
    def log_retention(self):
        """CloudWatch Logs RetentionDays enum for ``log_retention_days``."""
        from aws_cdk import aws_logs as logs

        mapping = {
            1: logs.RetentionDays.ONE_DAY,
            3: logs.RetentionDays.THREE_DAYS,
            5: logs.RetentionDays.FIVE_DAYS,
            7: logs.RetentionDays.ONE_WEEK,
            14: logs.RetentionDays.TWO_WEEKS,
            30: logs.RetentionDays.ONE_MONTH,
            60: logs.RetentionDays.TWO_MONTHS,
            90: logs.RetentionDays.THREE_MONTHS,
            180: logs.RetentionDays.SIX_MONTHS,
            365: logs.RetentionDays.ONE_YEAR,
        }
        return mapping.get(self.log_retention_days, logs.RetentionDays.ONE_MONTH)

    def source_lifecycle_transition_days(self):
        return Duration.days(self.glacier_transition_days)

    def source_lifecycle_expiry(self):
        return Duration.days(self.source_expiry_days)


def _as_bool(value: object, *, default: bool) -> bool:
    """Coerce a CDK context value (bool or string) to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, *, default: int) -> int:
    """Coerce a CDK context value (int or string) to int."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
