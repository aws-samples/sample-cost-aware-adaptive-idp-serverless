# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for models, line parsing, dedup hashing, and records."""

import pytest

from idp_common.dedup import content_hash
from idp_common.models import BBox, Document
from idp_common.record import (
    METHOD_LLM,
    METHOD_SPATIAL,
    METHOD_TEXTRACT,
    DocumentRecord,
)
from idp_common.text_lines import (
    LineSpan,
    line_to_key_value,
    page_lines_to_key_values,
)


# ----- models --------------------------------------------------------------
def test_document_roundtrip(employment_form):
    restored = Document.from_dict(employment_form.to_dict())
    assert restored.page_count == 1
    assert restored.is_born_digital
    assert len(restored.all_key_values()) == len(employment_form.all_key_values())


def test_is_born_digital_false_without_text_layer():
    doc = Document.from_dict({"pages": [{"key_values": [], "has_text_layer": False}]})
    assert not doc.is_born_digital


# ----- line -> key/value parsing --------------------------------------------
def _line(text):
    return LineSpan(text=text, bbox=BBox(0.1, 0.1, 0.2, 0.02))


def test_line_to_key_value_parses_label_colon_value():
    kv = line_to_key_value(_line("Invoice Number: INV-001"), page_number=1)
    assert kv is not None
    assert kv.key_text == "Invoice Number"
    assert kv.value == "INV-001"
    assert kv.page == 1


def test_line_to_key_value_ignores_non_kv_lines():
    assert line_to_key_value(_line("Just a sentence with no key"), 1) is None
    assert line_to_key_value(_line(""), 1) is None


def test_page_lines_to_key_values_filters():
    lines = [_line("Total: 100.00"), _line("Thank you"), _line("Tax: 5.00")]
    kvs = page_lines_to_key_values(lines, 1)
    assert {kv.key_text for kv in kvs} == {"Total", "Tax"}


# ----- dedup ---------------------------------------------------------------
def test_content_hash_is_stable_and_distinct():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


# ----- record / provenance --------------------------------------------------
def test_method_summary_and_paid_count():
    rec = DocumentRecord(doc_id="d1", doc_type="invoice", source_uri="s3://b/k")
    rec.add_field("Total", "100", METHOD_SPATIAL, 1.0)
    rec.add_field("Vendor", "ACME", METHOD_TEXTRACT, 0.9)
    rec.add_field("Summary", "x", METHOD_LLM, 0.8)
    assert rec.method_summary == {
        METHOD_SPATIAL: 1,
        METHOD_TEXTRACT: 1,
        METHOD_LLM: 1,
    }
    assert rec.paid_field_count == 2  # textract + llm
    assert rec.mean_confidence == pytest.approx((1.0 + 0.9 + 0.8) / 3)


def test_record_to_dict_shape():
    rec = DocumentRecord(doc_id="d1", doc_type="invoice", source_uri="s3://b/k")
    rec.add_field("Total", "100", METHOD_SPATIAL, 1.0, {"x": 0.6, "y": 0.8, "width": 0.1, "height": 0.03})
    out = rec.to_dict()
    assert out["fields"]["Total"] == "100"
    assert out["field_provenance"]["Total"]["method"] == METHOD_SPATIAL
    assert out["paid_field_count"] == 0
    assert "ingested_at" in out
