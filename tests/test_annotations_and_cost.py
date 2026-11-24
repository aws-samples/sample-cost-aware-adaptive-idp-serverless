# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the annotations adapter and the cost model tool."""

import sys
from pathlib import Path

from idp_common import annotations

# tools/ is not a package; add it to the path for the cost model import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cost_model  # noqa: E402


# ----- annotations ---------------------------------------------------------
def test_build_object_annotations():
    record = {
        "doc_id": "d1",
        "doc_type": "pile_test_report",
        "compliance_result": "PASS (rule=PASS)",
        "compression_algo": "gzip",
        "paid_field_count": 2,
        "entity_keys": {"project_id": "P-1"},
    }
    ann = annotations.build_object_annotations(record)
    assert ann["doc_type"] == "pile_test_report"
    assert ann["compliance"] == "PASS"  # overall status only
    assert ann["extraction_id"] == "d1"
    assert ann["project_id"] == "P-1"


def test_parse_s3_uri():
    assert annotations.parse_s3_uri("s3://bucket/a/b.pdf") == ("bucket", "a/b.pdf")
    assert annotations.parse_s3_uri("https://x/y") is None


def test_write_annotations_noop_when_disabled():
    calls = []

    class _S3:
        def put_object_tagging(self, **kw):
            calls.append(kw)

    wrote = annotations.write_annotations(_S3(), "b", "k", {"a": "1"}, enabled=False)
    assert wrote is False
    assert calls == []


def test_write_annotations_writes_when_enabled():
    calls = []

    class _S3:
        def put_object_tagging(self, **kw):
            calls.append(kw)

    wrote = annotations.write_annotations(_S3(), "b", "k", {"doc_type": "x"}, enabled=True)
    assert wrote is True
    assert calls[0]["Tagging"]["TagSet"] == [{"Key": "doc_type", "Value": "x"}]


# ----- cost model ----------------------------------------------------------
def test_low_cost_ocr_is_lowest_cost_extraction_tier():
    pages, model = 1.0, "nova-lite"
    costs = {
        tier: cost_model.tier_cost_per_doc(tier, pages, model)
        for tier in ("dedup", "low_cost_ocr", "llm", "bda", "structured", "full_cycle")
    }
    # A skipped duplicate is cheapest; among real extraction tiers cheap OCR
    # wins, and structured Textract is the priciest single OCR mode.
    assert costs["dedup"] < costs["low_cost_ocr"]
    assert costs["low_cost_ocr"] < costs["llm"] < costs["structured"]
    assert costs["low_cost_ocr"] < costs["bda"] < costs["structured"]
    assert costs["full_cycle"] > costs["structured"]


def test_blended_beats_naive_baseline():
    report = cost_model.compute(cost_model.CostInputs())
    assert report["blended_per_1000"] < report["naive_baseline_per_1000"]
    assert report["savings_vs_naive_pct"] > 50  # meaningful savings


def test_mix_must_sum_to_one():
    import pytest

    bad = cost_model.CostInputs(low_cost_ocr=0.5, llm=0.1, bda=0.1)
    with pytest.raises(ValueError):
        cost_model.compute(bad)


def test_sonnet_costs_more_than_nova_full_cycle():
    nova = cost_model.tier_cost_per_doc("full_cycle", 1.0, "nova-lite")
    sonnet = cost_model.tier_cost_per_doc("full_cycle", 1.0, "claude-sonnet")
    assert sonnet > nova


def test_format_report_runs():
    text = cost_model.format_report(cost_model.compute(cost_model.CostInputs()))
    assert "Blended" in text and "Savings vs naive" in text
