# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for grounding LLM values in OCR text (threat T1)."""

from idp_common import grounding
from idp_common.models import Document


def _doc(pairs):
    return Document.from_dict(
        {
            "pages": [
                {
                    "has_text_layer": True,
                    "key_values": [
                        {
                            "key": key.split(),
                            "bbox": {"x": 0.1, "y": 0.1 * i, "width": 0.2, "height": 0.02},
                            "page": 1,
                            "value": value,
                        }
                        for i, (key, value) in enumerate(pairs)
                    ],
                }
            ]
        }
    )


def test_normalize_label_and_value():
    assert grounding.normalize_label("Pile  Capacity:") == "pile_capacity"
    assert grounding.normalize_label("pile_capacity") == "pile_capacity"
    assert grounding.normalize_value("1,200 kN") == "1200 kn"


def test_ocr_index_groups_values_by_normalized_label():
    index = grounding.ocr_index(_doc([("Pile Capacity", "120 kN"), ("pile capacity", "130")]))
    assert index == {"pile_capacity": ["120 kN", "130"]}


def test_value_grounded_as_whole_token_sequence():
    index = grounding.ocr_index(_doc([("Pile Capacity", "120 kN"), ("Withstand Limit", "50")]))
    assert grounding.is_grounded("pile_capacity", "120", index)
    assert grounding.is_grounded("Pile Capacity", "120 kN", index)
    # "5" is not a token of "50".
    assert not grounding.is_grounded("withstand_limit", "5", index)


def test_value_not_grounded_when_absent_or_under_another_label():
    index = grounding.ocr_index(_doc([("Pile Capacity", "120"), ("Withstand Limit", "100")]))
    assert not grounding.is_grounded("pile_capacity", "999", index)  # injected value
    assert not grounding.is_grounded("pile_capacity", "100", index)  # wrong label
    assert not grounding.is_grounded("concrete_strength", "35", index)  # no such label
    assert not grounding.is_grounded("pile_capacity", "", index)


def test_commas_ignored_when_matching():
    index = grounding.ocr_index(_doc([("Total", "1,250.00 USD")]))
    assert grounding.is_grounded("total", "1250.00", index)


def test_empty_document_grounds_nothing():
    assert not grounding.is_grounded("pile_capacity", "120", grounding.ocr_index(Document()))


def test_unit_exponent_forms_ground_against_each_other():
    index = grounding.ocr_index(
        _doc([("Concrete Strength", "35 N/mm\u00b2"), ("Operating Pressure", "850 kN/m^2")])
    )
    assert grounding.is_grounded("concrete_strength", "35", index)
    assert grounding.is_grounded("concrete_strength", "35 N/mm2", index)
    assert grounding.is_grounded("operating_pressure", "850 kN/m2", index)
    # The unit exponent is part of the unit token, never a value on its own.
    assert not grounding.is_grounded("concrete_strength", "2", index)
    assert not grounding.is_grounded("operating_pressure", "2", index)


def test_strip_unit_exponents():
    assert grounding.strip_unit_exponents("35 N/mm2") == "35 N/mm"
    assert grounding.strip_unit_exponents("12 m3, 4 cm3") == "12 m, 4 cm"
    assert grounding.strip_unit_exponents("35 N/mm\u00b2") == "35 N/mm"
    assert grounding.strip_unit_exponents("35 N/mm^2.") == "35 N/mm."
    # Ranges, grades and multi-digit suffixes are left alone.
    assert grounding.strip_unit_exponents("30-35") == "30-35"
    assert grounding.strip_unit_exponents("C30/37") == "C30/37"
    assert grounding.strip_unit_exponents("P-14") == "P-14"


def test_all_verified():
    assert grounding.all_verified({"a": {"grounded": True}, "b": {"grounded": None}})
    assert not grounding.all_verified({"a": {"grounded": True}, "b": {"grounded": False}})
    assert not grounding.all_verified({})  # nothing kept -> nothing verified


def test_all_verified_ignores_empty_values_when_fields_given():
    prov = {"a": {"grounded": True}, "empty": {"grounded": False}, "none": {"grounded": False}}
    fields = {"a": "120 kN", "empty": "  ", "none": None}
    assert not grounding.all_verified(prov)  # without values, every field counts
    assert grounding.all_verified(prov, fields=fields)
    # A populated value without grounding still fails closed.
    assert not grounding.all_verified(prov, fields={**fields, "empty": "P-91"})
    # Only empty values -> nothing verified.
    assert not grounding.all_verified({"empty": {"grounded": False}}, fields={"empty": ""})
