# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the rebuilt spatial field-memory engine (file store)."""

from pathlib import Path

import pytest

from idp_common.template_memory import FileTemplateStore, TemplateMemory
from idp_common.template_memory.models import FieldLocationMap, FieldRegion


@pytest.fixture
def memory(tmp_path):
    store = FileTemplateStore(tmp_path / "templates")
    return TemplateMemory(store=store, similarity_threshold=0.5)


def test_record_creates_template(memory, employment_form):
    template_id = memory.record(employment_form, template_id="employment-form")
    assert template_id == "employment-form"
    tmpl = memory.get_template("employment-form")
    assert tmpl is not None
    assert "Employee Name" in tmpl.fields
    assert tmpl.sample_count == 1


def test_record_refines_positions_over_samples(memory, employment_form):
    for _ in range(5):
        memory.record(employment_form, template_id="employment-form")
    tmpl = memory.get_template("employment-form")
    assert tmpl.sample_count == 5
    region = tmpl.get_field_region("Employee Name")
    assert region.occurrence_count == 5


def test_identify_distinguishes_doc_types(memory, employment_form, invoice):
    for _ in range(3):
        memory.record(employment_form, template_id="employment-form")
        memory.record(invoice, template_id="invoice")

    assert memory.identify_template(employment_form).template_id == "employment-form"
    assert memory.identify_template(invoice).template_id == "invoice"


def test_locate_finds_field_in_expected_region(memory, employment_form):
    for _ in range(3):
        memory.record(employment_form, template_id="employment-form")
    matches = memory.locate(employment_form, "Employee Name")
    assert matches
    top = matches[0]
    assert top.key_value.key_text == "Employee Name"
    assert top.within_expected_region


def test_auto_template_id_when_unspecified(memory, invoice):
    template_id = memory.record(invoice)
    assert template_id  # a signature was minted
    assert template_id in memory.list_templates()


def test_record_rejects_empty_document(memory):
    from idp_common.models import Document

    with pytest.raises(ValueError):
        memory.record(Document(pages=[]))


def test_delete_template(memory, invoice):
    memory.record(invoice, template_id="invoice")
    assert "invoice" in memory.list_templates()
    memory.delete_template("invoice")
    assert "invoice" not in memory.list_templates()


def test_field_location_map_roundtrip():
    flm = FieldLocationMap(template_id="t1", page_count=1)
    flm.add_field(
        "Total",
        FieldRegion("Total", 1, {"x": 0.6, "y": 0.8, "width": 0.1, "height": 0.03}, 0.9),
    )
    restored = FieldLocationMap.from_dict(flm.to_dict())
    assert restored.template_id == "t1"
    assert restored.get_field_region("Total").confidence == pytest.approx(0.9)


def test_record_stamps_and_preserves_doc_type(memory, employment_form):
    # The LLM/BDA tier learns a template tagged with its classified doc type.
    memory.record(employment_form, template_id="t-dt", doc_type="lab_result_report")
    assert memory.get_template("t-dt").doc_type == "lab_result_report"
    # A later free spatial re-observation (no doc_type) must not clear it.
    memory.record(employment_form, template_id="t-dt")
    assert memory.get_template("t-dt").doc_type == "lab_result_report"


def test_merge_rejects_mismatched_template_ids():
    a = FieldLocationMap(template_id="a")
    b = FieldLocationMap(template_id="b")
    with pytest.raises(ValueError):
        a.merge(b, tolerance=0.05)


# ----- template trust (threat T2) --------------------------------------------
from idp_common.models import BBox, Document, KeyValue, Page, Word  # noqa: E402
from idp_common.template_memory import compare, select_for_spot_check  # noqa: E402
from idp_common.template_memory import spot_check as spot_check_mod  # noqa: E402


def test_legacy_template_without_trust_loads_as_provisional():
    flm = FieldLocationMap(template_id="legacy")
    data = flm.to_dict()
    del data["trust_state"]
    del data["confirmations"]
    restored = FieldLocationMap.from_dict(data)
    assert restored.trust_state == "provisional"
    assert restored.confirmations == 0


def test_trust_state_roundtrips():
    flm = FieldLocationMap(template_id="t", trust_state="trusted", confirmations=3)
    restored = FieldLocationMap.from_dict(flm.to_dict())
    assert (restored.trust_state, restored.confirmations) == ("trusted", 3)


def test_first_ai_document_creates_provisional_template(memory, invoice):
    template_id, state = memory.learn_from_ai(invoice, doc_type="invoice")
    assert state == "provisional"
    tmpl = memory.get_template(template_id)
    assert tmpl.confirmations == 1
    assert tmpl.doc_type == "invoice"


def test_second_agreeing_ai_document_makes_template_trusted(memory, invoice):
    first, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    second, state = memory.learn_from_ai(invoice, doc_type="invoice")
    assert second == first
    assert state == "trusted"
    assert memory.get_template(first).confirmations == 2


def test_confirmations_required_three(memory, invoice):
    states = [memory.learn_from_ai(invoice, confirmations_required=3)[1] for _ in range(3)]
    assert states == ["provisional", "provisional", "trusted"]


def test_confirmations_required_one_trusts_immediately(memory, invoice):
    assert memory.learn_from_ai(invoice, confirmations_required=1)[1] == "trusted"


def test_doc_type_disagreement_resets_to_provisional(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    memory.learn_from_ai(invoice, doc_type="invoice")
    same_id, state = memory.learn_from_ai(invoice, doc_type="receipt")
    assert same_id == template_id
    assert state == "provisional"
    tmpl = memory.get_template(template_id)
    assert tmpl.confirmations == 1
    assert tmpl.doc_type == "receipt"


def test_identify_trusted_only_ignores_provisional(memory, invoice):
    memory.learn_from_ai(invoice, doc_type="invoice")
    assert memory.identify_template(invoice) is not None
    assert memory.identify_template(invoice, trusted_only=True) is None
    memory.learn_from_ai(invoice, doc_type="invoice")
    assert memory.identify_template(invoice, trusted_only=True) is not None


def test_refine_from_spatial_never_creates(memory, invoice):
    assert memory.refine_from_spatial(invoice, "missing") is False
    assert memory.list_templates() == []


def test_refine_from_spatial_noop_on_provisional(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    before = memory.get_template(template_id).sample_count
    assert memory.refine_from_spatial(invoice, template_id) is False
    assert memory.get_template(template_id).sample_count == before


def test_refine_from_spatial_keeps_doc_type_and_trust(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    memory.learn_from_ai(invoice, doc_type="invoice")
    assert memory.refine_from_spatial(invoice, template_id) is True
    tmpl = memory.get_template(template_id)
    assert (tmpl.doc_type, tmpl.trust_state, tmpl.confirmations) == ("invoice", "trusted", 2)
    assert tmpl.sample_count == 3
    assert len(memory.list_templates()) == 1


def test_demote(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, confirmations_required=1)
    assert memory.demote(template_id) is True
    tmpl = memory.get_template(template_id)
    assert (tmpl.trust_state, tmpl.confirmations) == ("provisional", 0)
    assert memory.demote("missing") is False


def test_select_for_spot_check_bounds():
    assert select_for_spot_check(0) is False
    assert select_for_spot_check(-5) is False
    assert select_for_spot_check(100) is True
    assert select_for_spot_check(150) is True


def test_select_for_spot_check_uses_secrets(monkeypatch):
    # 5 % -> draws below 500 (of 10000) are selected.
    monkeypatch.setattr(spot_check_mod.secrets, "randbelow", lambda _n: 499)
    assert select_for_spot_check(5) is True
    monkeypatch.setattr(spot_check_mod.secrets, "randbelow", lambda _n: 500)
    assert select_for_spot_check(5) is False


def test_compare_agree_and_disagree():
    doc = Document(pages=[Page(has_text_layer=True, key_values=[
        KeyValue(key=[Word(t) for t in name.split()], bbox=BBox(0.1, y, 0.2, 0.02),
                 page=1, value=value)
        for name, y, value in (("Pile Capacity", 0.1, "1,200"), ("Withstand Limit", 0.2, "100"))
    ])])
    agree, _ = compare(doc, "pile_test_report", "pile_test_report",
                       {"pile_capacity": "1200", "project_id": "P-1"})
    assert agree is True
    # Unknown doc types on either side are not a disagreement.
    assert compare(doc, None, "pile_test_report", {"pile_capacity": "1200"})[0] is True
    agree, reason = compare(doc, "pile_test_report", "invoice", {})
    assert agree is False and "doc_type" in reason
    agree, reason = compare(doc, "pile_test_report", "pile_test_report",
                            {"withstand_limit": "999"})
    assert agree is False and "withstand_limit" in reason


# ----- only verified AI documents earn trust (threat T2) --------------------
@pytest.mark.parametrize("required", [1, 2, 3])
def test_unverified_documents_never_make_a_template_trusted(memory, invoice, required):
    for _ in range(required + 3):
        template_id, state = memory.learn_from_ai(
            invoice, doc_type="invoice", confirmations_required=required, verified=False
        )
        assert state == "provisional"
    tmpl = memory.get_template(template_id)
    assert (tmpl.trust_state, tmpl.confirmations) == ("provisional", 0)
    assert memory.identify_template(invoice, trusted_only=True) is None
    assert len(memory.list_templates()) == 1


def test_unverified_document_creates_provisional_template_and_merges_regions(memory, invoice):
    template_id, state = memory.learn_from_ai(invoice, doc_type="invoice", verified=False)
    assert state == "provisional"
    tmpl = memory.get_template(template_id)
    assert (tmpl.doc_type, tmpl.confirmations, tmpl.sample_count) == ("invoice", 0, 1)
    memory.learn_from_ai(invoice, doc_type="invoice", verified=False)
    assert memory.get_template(template_id).sample_count == 2


@pytest.mark.parametrize("required", [2, 3])
def test_verified_documents_trust_at_configured_count(memory, invoice, required):
    # Unverified documents interleaved with verified ones add no confirmations.
    memory.learn_from_ai(invoice, doc_type="invoice", confirmations_required=required,
                         verified=False)
    states = []
    for _ in range(required):
        states.append(memory.learn_from_ai(
            invoice, doc_type="invoice", confirmations_required=required
        )[1])
        memory.learn_from_ai(invoice, doc_type="invoice", confirmations_required=required,
                             verified=False)
    assert states == ["provisional"] * (required - 1) + ["trusted"]
    tmpl = memory.get_template(memory.list_templates()[0])
    assert (tmpl.trust_state, tmpl.confirmations) == ("trusted", required)


def test_unverified_document_cannot_retag_or_touch_trusted_template(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    memory.learn_from_ai(invoice, doc_type="invoice")
    before = memory.get_template(template_id).sample_count
    same_id, state = memory.learn_from_ai(invoice, doc_type="receipt", verified=False)
    assert (same_id, state) == (template_id, "trusted")
    tmpl = memory.get_template(template_id)
    assert (tmpl.doc_type, tmpl.confirmations, tmpl.sample_count) == ("invoice", 2, before)


def test_unverified_document_cannot_retag_provisional_template(memory, invoice):
    template_id, _ = memory.learn_from_ai(invoice, doc_type="invoice")
    memory.learn_from_ai(invoice, doc_type="receipt", verified=False)
    tmpl = memory.get_template(template_id)
    assert (tmpl.doc_type, tmpl.confirmations, tmpl.sample_count) == ("invoice", 1, 1)
