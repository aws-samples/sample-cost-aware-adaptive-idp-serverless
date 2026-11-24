# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for CDK-context configuration resolution (cost guardrail knobs)."""

import aws_cdk as cdk

from infra.config import AppConfig


def test_cost_guardrail_defaults():
    config = AppConfig.from_context(cdk.App())
    assert config.max_file_size_mb == 50
    assert config.max_pages == 200
    assert config.paid_tier_max_concurrency == 5
    assert config.monthly_budget_usd == 100
    assert config.alert_email == ""


def test_cost_guardrail_overrides():
    app = cdk.App(
        context={
            "idp:maxFileSizeMb": 10,
            "idp:maxPages": 20,
            "idp:paidTierMaxConcurrency": 0,
            "idp:monthlyBudgetUsd": 250,
            "idp:alertEmail": "ops@example.com",
        }
    )
    config = AppConfig.from_context(app)
    assert config.max_file_size_mb == 10
    assert config.max_pages == 20
    assert config.paid_tier_max_concurrency == 0
    assert config.monthly_budget_usd == 250
    assert config.alert_email == "ops@example.com"


def test_cost_guardrail_string_coercion():
    # ``-c key=value`` overrides arrive as strings.
    app = cdk.App(
        context={
            "idp:maxFileSizeMb": "25",
            "idp:maxPages": "not-a-number",
            "idp:paidTierMaxConcurrency": "-3",
            "idp:monthlyBudgetUsd": "75",
            "idp:alertEmail": "  ops@example.com  ",
        }
    )
    config = AppConfig.from_context(app)
    assert config.max_file_size_mb == 25
    assert config.max_pages == 200  # invalid -> default
    assert config.paid_tier_max_concurrency == 0  # negatives clamp to 0
    assert config.monthly_budget_usd == 75
    assert config.alert_email == "ops@example.com"


def test_bedrock_guardrail_defaults_off_and_can_be_enabled():
    assert AppConfig.from_context(cdk.App()).bedrock_guardrail_enabled is False
    for value in (True, "true"):
        app = cdk.App(context={"idp:bedrockGuardrailEnabled": value})
        assert AppConfig.from_context(app).bedrock_guardrail_enabled is True


def test_retrieval_defaults_off_and_can_be_enabled():
    assert AppConfig.from_context(cdk.App()).retrieval_enabled is False
    for value in (True, "true"):
        app = cdk.App(context={"idp:retrievalEnabled": value})
        assert AppConfig.from_context(app).retrieval_enabled is True
    app = cdk.App(context={"idp:retrievalEnabled": "false"})
    assert AppConfig.from_context(app).retrieval_enabled is False


def test_template_trust_knobs_defaults_overrides_and_clamping():
    config = AppConfig.from_context(cdk.App())
    assert config.template_confirmations == 2
    assert config.spot_check_percent == 5
    app = cdk.App(context={"idp:templateConfirmations": "3", "idp:spotCheckPercent": 20})
    config = AppConfig.from_context(app)
    assert (config.template_confirmations, config.spot_check_percent) == (3, 20)
    app = cdk.App(context={"idp:templateConfirmations": 0, "idp:spotCheckPercent": 250})
    config = AppConfig.from_context(app)
    assert (config.template_confirmations, config.spot_check_percent) == (1, 100)
    app = cdk.App(context={"idp:spotCheckPercent": -1, "idp:templateConfirmations": "x"})
    config = AppConfig.from_context(app)
    assert (config.template_confirmations, config.spot_check_percent) == (2, 0)
