# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for Textract parsing and tier escalation logic."""

from idp_common import textract as tx
from idp_common.escalation import assess_ocr_clarity
from idp_common.template_memory import FileTemplateStore, TemplateMemory


def _geo(left, top, width, height):
    return {"BoundingBox": {"Left": left, "Top": top, "Width": width, "Height": height}}


def test_parse_detect_text_recovers_key_values():
    response = {
        "Blocks": [
            {"BlockType": "LINE", "Text": "Invoice Number: INV-9", "Page": 1,
             "Geometry": _geo(0.1, 0.1, 0.3, 0.02)},
            {"BlockType": "LINE", "Text": "Total: 42.00", "Page": 1,
             "Geometry": _geo(0.1, 0.2, 0.2, 0.02)},
            {"BlockType": "LINE", "Text": "Thank you for your business", "Page": 1,
             "Geometry": _geo(0.1, 0.9, 0.5, 0.02)},
            {"BlockType": "WORD", "Text": "ignored"},
        ]
    }
    doc = tx.parse_detect_text(response)
    assert doc.page_count == 1
    names = {kv.key_text for kv in doc.all_key_values()}
    assert names == {"Invoice Number", "Total"}


def test_parse_detect_text_multipage():
    response = {
        "Blocks": [
            {"BlockType": "LINE", "Text": "A: 1", "Page": 1, "Geometry": _geo(0.1, 0.1, 0.2, 0.02)},
            {"BlockType": "LINE", "Text": "B: 2", "Page": 2, "Geometry": _geo(0.1, 0.1, 0.2, 0.02)},
        ]
    }
    doc = tx.parse_detect_text(response)
    assert doc.page_count == 2


def test_parse_analyze_forms_links_key_to_value():
    response = {
        "Blocks": [
            {
                "Id": "k1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"],
                "Page": 1, "Confidence": 99.0, "Geometry": _geo(0.1, 0.1, 0.2, 0.02),
                "Relationships": [
                    {"Type": "CHILD", "Ids": ["wk1"]},
                    {"Type": "VALUE", "Ids": ["v1"]},
                ],
            },
            {"Id": "wk1", "BlockType": "WORD", "Text": "Vendor"},
            {
                "Id": "v1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"],
                "Page": 1, "Geometry": _geo(0.3, 0.1, 0.2, 0.02),
                "Relationships": [{"Type": "CHILD", "Ids": ["wv1"]}],
            },
            {"Id": "wv1", "BlockType": "WORD", "Text": "ACME"},
        ]
    }
    doc = tx.parse_analyze_forms(response)
    kvs = doc.all_key_values()
    assert len(kvs) == 1
    assert kvs[0].key_text == "Vendor"
    assert kvs[0].value == "ACME"
    assert kvs[0].confidence == 0.99


def test_count_tables():
    response = {"Blocks": [{"BlockType": "TABLE"}, {"BlockType": "TABLE"}, {"BlockType": "LINE"}]}
    assert tx.count_tables(response) == 2


# ----- escalation ----------------------------------------------------------
def test_clarity_escalates_when_too_few_fields(employment_form):
    response = {"Blocks": [{"BlockType": "LINE", "Text": "X: 1", "Page": 1,
                            "Geometry": _geo(0.1, 0.1, 0.2, 0.02)}]}
    doc = tx.parse_detect_text(response)
    decision = assess_ocr_clarity(doc, None)
    assert decision.is_clear is False
    assert "field" in decision.reason


def test_clarity_clear_when_template_matches(tmp_path, employment_form):
    memory = TemplateMemory(store=FileTemplateStore(tmp_path), similarity_threshold=0.5)
    for _ in range(3):
        memory.record(employment_form, template_id="employment-form")
    match = memory.identify_template(employment_form)
    decision = assess_ocr_clarity(employment_form, match)
    assert decision.is_clear is True
    assert decision.template_matched
