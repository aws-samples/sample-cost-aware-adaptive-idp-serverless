# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for industry pack loading, compliance, and reconciliation."""

import json

import pytest

from idp_common import compliance
from idp_common.packs import (
    list_packs,
    load_pack,
    load_pack_data,
    load_pack_dynamodb,
    pack_from_item,
    resolve_pack,
)


def test_construction_pack_loads():
    pack = load_pack("construction")
    assert pack.industry == "construction"
    assert "project_id" in pack.linking_keys
    assert any(r.id == "pile_capacity_meets_withstand" for r in pack.compliance_rules)
    assert pack.value_at_risk_weights["pile_capacity"] == 1.0


EXPECTED_PACKS = {
    "construction",
    "energy",
    "healthcare",
}


def test_list_packs_includes_all_industries():
    assert EXPECTED_PACKS.issubset(set(list_packs()))


@pytest.mark.parametrize("name", sorted(EXPECTED_PACKS))
def test_every_pack_loads_and_is_well_formed(name):
    pack = load_pack(name)
    assert pack.industry
    assert pack.linking_keys, f"{name} should define linking keys"
    assert pack.compliance_rules, f"{name} should define compliance rules"
    # Every compliance rule expr must contain a supported operator.
    for rule in pack.compliance_rules:
        assert any(op in rule.expr for op in ("<=", ">=", "==", "!=", "<", ">"))


def test_pack_specific_rule_runs_without_engine_change():
    # Energy pressure-within-rating check, evaluated by the same generic engine.
    pack = load_pack("energy")
    ok = compliance.evaluate_compliance(
        {"operating_pressure": "100", "pressure_rating": "150"},
        pack, doc_type="well_test_report",
    )
    bad = compliance.evaluate_compliance(
        {"operating_pressure": "200", "pressure_rating": "150"},
        pack, doc_type="well_test_report",
    )
    statuses_ok = {r.rule_id: r.status for r in ok.rules}
    assert statuses_ok["pressure_within_rating"] == compliance.COMPLIANT
    statuses_bad = {r.rule_id: r.status for r in bad.rules}
    assert statuses_bad["pressure_within_rating"] == compliance.FAIL


def test_list_packs_includes_construction():
    assert "construction" in list_packs()


def test_load_missing_pack_raises():
    with pytest.raises(FileNotFoundError):
        load_pack("does-not-exist")


# ----- compliance ----------------------------------------------------------
def test_compliance_pass_when_capacity_meets_limit():
    pack = load_pack("construction")
    fields = {"Pile Capacity": "120", "Withstand Limit": "100", "Concrete Strength": "35 MPa"}
    report = compliance.evaluate_compliance(fields, pack)
    assert report.overall == compliance.COMPLIANT
    statuses = {r.rule_id: r.status for r in report.rules}
    assert statuses["pile_capacity_meets_withstand"] == compliance.COMPLIANT
    assert statuses["concrete_strength_minimum"] == compliance.COMPLIANT


def test_compliance_fail_when_capacity_below_limit():
    pack = load_pack("construction")
    fields = {"pile_capacity": "80", "withstand_limit": "100"}
    report = compliance.evaluate_compliance(fields, pack)
    assert report.overall == compliance.FAIL


def test_compliance_na_when_fields_missing():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance({"unrelated": "x"}, pack)
    assert report.overall == compliance.NA


# ----- NEEDS_REVIEW: unverified / ambiguous values (threat T1) --------------
def _pile_statuses(report):
    return {r.rule_id: r.status for r in report.rules}


def test_unverified_value_that_passes_needs_review():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"Pile Capacity": "120", "withstand_limit": "100"},
        pack,
        doc_type="pile_test_report",
        unverified_fields=["pile_capacity"],
    )
    assert _pile_statuses(report)["pile_capacity_meets_withstand"] == compliance.NEEDS_REVIEW
    assert report.overall == compliance.NEEDS_REVIEW
    assert report.to_summary().startswith("NEEDS_REVIEW")


def test_unverified_name_matching_is_case_and_space_insensitive():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"pile_capacity": "120", "withstand_limit": "100"},
        pack,
        doc_type="pile_test_report",
        unverified_fields=["Pile Capacity"],
    )
    assert report.overall == compliance.NEEDS_REVIEW


def test_unverified_value_that_fails_stays_fail():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"pile_capacity": "80", "withstand_limit": "100"},
        pack,
        doc_type="pile_test_report",
        unverified_fields=["pile_capacity", "withstand_limit"],
    )
    assert _pile_statuses(report)["pile_capacity_meets_withstand"] == compliance.FAIL
    assert report.overall == compliance.FAIL


@pytest.mark.parametrize("value", ["TBD", "approx 500 or 1000", "30-35"])
def test_value_not_exactly_one_number_needs_review(value):
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"pile_capacity": value, "withstand_limit": "100"}, pack, doc_type="pile_test_report"
    )
    assert _pile_statuses(report)["pile_capacity_meets_withstand"] == compliance.NEEDS_REVIEW
    assert report.overall == compliance.NEEDS_REVIEW


def test_value_with_units_and_commas_still_parses():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"pile_capacity": "1,200 kN", "withstand_limit": "1,000 kN"},
        pack,
        doc_type="pile_test_report",
    )
    assert _pile_statuses(report)["pile_capacity_meets_withstand"] == compliance.COMPLIANT


@pytest.mark.parametrize(
    "value", ["35 N/mm2", "35N/mm2", "35 N/mm\u00b2", "35 N/mm^2", "35 N/mm2.", "35 MPa"]
)
def test_unit_exponent_is_not_a_second_number_construction(value):
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"concrete_strength": value}, pack, doc_type="pile_test_report"
    )
    assert _pile_statuses(report)["concrete_strength_minimum"] == compliance.COMPLIANT
    failing = compliance.evaluate_compliance(
        {"concrete_strength": value.replace("35", "25")}, pack, doc_type="pile_test_report"
    )
    assert _pile_statuses(failing)["concrete_strength_minimum"] == compliance.FAIL


@pytest.mark.parametrize(
    "operating, rating, expected",
    [
        ("850 kN/m2", "1,000 kN/m2", compliance.COMPLIANT),
        ("8.5 kg/cm2", "10 kg/cm2", compliance.COMPLIANT),
        ("1,450 lbf/in2", "1,440 lbf/in2", compliance.FAIL),
        ("12 m3", "15 m3", compliance.COMPLIANT),
        ("40 cm3", "30 cm3", compliance.FAIL),
        ("1.2 m2", "2 m2", compliance.COMPLIANT),
    ],
)
def test_unit_exponent_is_not_a_second_number_energy(operating, rating, expected):
    pack = load_pack("energy")
    report = compliance.evaluate_compliance(
        {"operating_pressure": operating, "pressure_rating": rating},
        pack,
        doc_type="well_test_report",
    )
    assert {r.rule_id: r.status for r in report.rules}["pressure_within_rating"] == expected


@pytest.mark.parametrize(
    "value",
    [
        "30-35 N/mm2",
        "approx 500 or 1000 kN/m2",
        "TBD N/mm2",
        "35 N/mm2 or 40 N/mm2",
        "C30/37",
        "2 x 35 N/mm2",
    ],
)
def test_ambiguous_values_with_unit_exponents_still_need_review(value):
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"concrete_strength": value}, pack, doc_type="pile_test_report"
    )
    assert _pile_statuses(report)["concrete_strength_minimum"] == compliance.NEEDS_REVIEW


def test_missing_field_stays_na():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {"pile_capacity": "120"},
        pack,
        doc_type="pile_test_report",
        unverified_fields=["pile_capacity"],
    )
    assert _pile_statuses(report)["pile_capacity_meets_withstand"] == compliance.NA
    assert report.overall == compliance.NA


@pytest.mark.parametrize(
    "fields, unverified, expected",
    [
        # FAIL beats NEEDS_REVIEW.
        ({"pile_capacity": "120", "withstand_limit": "100", "concrete_strength": "20"},
         ["pile_capacity"], compliance.FAIL),
        # NEEDS_REVIEW beats PASS.
        ({"pile_capacity": "120", "withstand_limit": "100", "concrete_strength": "35"},
         ["concrete_strength"], compliance.NEEDS_REVIEW),
        # NEEDS_REVIEW (invalid) beats PASS.
        ({"pile_capacity": "120", "withstand_limit": "100", "concrete_strength": "TBD"},
         [], compliance.NEEDS_REVIEW),
        # PASS beats NA.
        ({"pile_capacity": "120", "withstand_limit": "100"}, [], compliance.COMPLIANT),
        # All NA -> NA.
        ({"unrelated": "x"}, [], compliance.NA),
    ],
)
def test_overall_precedence(fields, unverified, expected):
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        fields, pack, doc_type="pile_test_report", unverified_fields=unverified
    )
    assert report.overall == expected


def test_force_review_sends_every_rule_to_review():
    pack = load_pack("construction")
    report = compliance.evaluate_compliance(
        {}, pack, doc_type="pile_test_report", force_review=True
    )
    assert report.rules
    assert {r.status for r in report.rules} == {compliance.NEEDS_REVIEW}
    assert report.overall == compliance.NEEDS_REVIEW


def test_force_review_without_rules_is_needs_review():
    # A guardrail-blocked document has no rules to mark but still needs review.
    pack = load_pack("construction")
    for doc_type in ("material_delivery_note", "unknown"):
        report = compliance.evaluate_compliance({}, pack, doc_type=doc_type, force_review=True)
        assert report.rules == []
        assert report.overall == compliance.NEEDS_REVIEW
        assert report.to_summary() == "NEEDS_REVIEW (guardrail=intervened)"
    plain = compliance.evaluate_compliance({}, pack, doc_type="unknown")
    assert (plain.overall, plain.to_summary()) == (compliance.NA, compliance.NA)


def test_extract_linking_keys_case_insensitive():
    pack = load_pack("construction")
    fields = {"Project ID": "P-1", "Asset Id": "A-9", "noise": "z"}
    keys = compliance.extract_linking_keys(fields, pack)
    assert keys == {"project_id": "P-1", "asset_id": "A-9"}


def test_reconcile_detects_conflicts():
    pack = load_pack("construction")
    records = [
        {"fields": {"project_id": "P-1", "pile_capacity": "120"}},
        {"fields": {"project_id": "P-1", "pile_capacity": "130"}},  # conflict
    ]
    result = compliance.reconcile_records(records, pack)
    assert result["linked_group_count"] == 1
    assert any(c["field"] == "pile_capacity" for c in result["conflicts"])


def test_reconcile_no_conflict_for_unlinked():
    pack = load_pack("construction")
    records = [
        {"fields": {"project_id": "P-1", "x": "1"}},
        {"fields": {"project_id": "P-2", "x": "2"}},
    ]
    result = compliance.reconcile_records(records, pack)
    assert result["conflicts"] == []


# ----- BDA blueprint schema derived from packs ------------------------------
def test_extraction_fields_merges_keys_rules_and_weights():
    pack = load_pack("construction")
    fields = pack.extraction_fields()
    # Linking keys, compliance-referenced fields, and weighted fields, no dupes.
    assert "project_id" in fields
    assert "pile_capacity" in fields  # from the compliance expr
    assert "withstand_limit" in fields
    assert "concrete_strength" in fields
    assert len(fields) == len({f.lower() for f in fields})  # de-duplicated
    # Pure numeric literals in exprs must not become fields.
    assert "30" not in fields


def test_build_blueprint_schema_shape():
    from idp_common import bda

    schema = bda.build_blueprint_schema(["project_id", "pile_capacity"])
    assert schema["class"] == "Document"
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["project_id"]["inferenceType"] == "explicit"
    # A generic inferred summary is always appended.
    assert props["summary"]["inferenceType"] == "inferred"


def test_build_blueprint_schema_includes_catch_all_fields():
    from idp_common import bda

    # Even a pack with a narrow field list still gets the common cross-industry
    # metadata fields on the BDA (visual) tier, since BDA has no "extract
    # anything else" fallback like the LLM prompt does.
    schema = bda.build_blueprint_schema(["pile_capacity"])
    props = schema["properties"]
    for name in bda.CATCH_ALL_FIELDS:
        assert name in props
        assert props[name]["inferenceType"] == "inferred"


def test_build_blueprint_schema_catch_all_can_be_disabled():
    from idp_common import bda

    schema = bda.build_blueprint_schema(["pile_capacity"], include_catch_all=False)
    assert "document_date" not in schema["properties"]


def test_build_blueprint_schema_uses_field_descriptions():
    from idp_common import bda

    schema = bda.build_blueprint_schema(
        ["withstand_limit"],
        field_descriptions={"withstand_limit": "also called design load"},
    )
    assert "also called design load" in schema["properties"]["withstand_limit"]["instruction"]


@pytest.mark.parametrize("name", sorted(EXPECTED_PACKS))
def test_every_pack_yields_blueprint_fields(name):
    from idp_common import bda

    pack = load_pack(name)
    fields = pack.extraction_fields()
    assert fields, f"{name} should yield extraction fields"
    schema = bda.build_blueprint_schema(fields)
    # Every derived field is present as an explicit property.
    for field_name in fields:
        assert schema["properties"][field_name]["inferenceType"] == "explicit"


# ----- pack config from DynamoDB (runtime source of truth) ------------------
def test_pack_from_item_parses_json_definition():
    data = load_pack_data("construction")
    pack = pack_from_item({"pack_name": "construction", "definition": json.dumps(data)})
    assert pack.industry == "construction"
    assert "project_id" in pack.linking_keys


def test_pack_from_item_accepts_dict_definition():
    data = load_pack_data("energy")
    pack = pack_from_item({"pack_name": "energy", "definition": data})
    assert pack.industry == "energy"


def test_resolve_pack_falls_back_to_bundled_without_table():
    # No table configured -> bundled YAML.
    pack = resolve_pack("construction", table_name=None)
    assert pack.industry == "construction"


def test_load_pack_and_resolve_pack_from_dynamodb():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="packs",
            AttributeDefinitions=[{"AttributeName": "pack_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pack_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        data = load_pack_data("construction")
        ddb.put_item(
            TableName="packs",
            Item={
                "pack_name": {"S": "construction"},
                "definition": {"S": json.dumps(data)},
            },
        )
        # Direct DDB load.
        pack = load_pack_dynamodb("construction", "packs", client=ddb)
        assert pack.industry == "construction"
        assert any(
            r.id == "pile_capacity_meets_withstand" for r in pack.compliance_rules
        )
        # resolve_pack prefers DDB when the table is set.
        resolved = resolve_pack("construction", table_name="packs", client=ddb)
        assert resolved.industry == "construction"


# ----- per-document-type fields and compliance -----------------------------
# ----- field descriptions/synonyms (author hints) ---------------------------
def test_field_can_be_a_bare_string_or_a_described_object():
    pack = load_pack("construction")
    pile = pack.document_type("pile_test_report")
    # Bare-string fields still parse as plain names.
    assert "test_date" in pile.fields
    # Object-form fields ({name, description}) parse into fields + descriptions.
    assert "withstand_limit" in pile.fields
    assert "withstand_limit" in pile.field_descriptions
    assert "design load" in pile.field_descriptions["withstand_limit"].lower()


def test_descriptions_for_is_scoped_by_doc_type():
    pack = load_pack("construction")
    pile_desc = pack.descriptions_for("pile_test_report")
    delivery_desc = pack.descriptions_for("material_delivery_note")
    assert "withstand_limit" in pile_desc
    # material_delivery_note fields carry no descriptions in this pack.
    assert "withstand_limit" not in delivery_desc


def test_descriptions_for_without_doc_type_merges_all_types():
    pack = load_pack("energy")
    merged = pack.descriptions_for()
    assert "operating_pressure" in merged  # well_test_report
    assert "wall_thickness" in merged  # pipeline_inspection


def test_document_types_have_their_own_fields():
    pack = load_pack("construction")
    pile = pack.document_type("pile_test_report")
    delivery = pack.document_type("material_delivery_note")
    assert pile is not None and delivery is not None
    assert "pile_capacity" in pile.fields
    # A delivery note does NOT carry pile fields — each type has its own set.
    assert "pile_capacity" not in delivery.fields
    assert "delivery_date" in delivery.fields


def test_fields_for_scopes_to_the_document_type():
    pack = load_pack("construction")
    pile_fields = pack.fields_for("pile_test_report")
    delivery_fields = pack.fields_for("material_delivery_note")
    # Linking keys are included for every type.
    assert "project_id" in pile_fields and "project_id" in delivery_fields
    # Type-specific fields are not shared.
    assert "pile_capacity" in pile_fields
    assert "pile_capacity" not in delivery_fields


def test_compliance_rules_scoped_by_doc_type():
    pack = load_pack("construction")
    pile_rules = {r.id for r in pack.compliance_rules_for("pile_test_report")}
    delivery_rules = {r.id for r in pack.compliance_rules_for("material_delivery_note")}
    assert "pile_capacity_meets_withstand" in pile_rules
    assert delivery_rules == set()  # delivery note has no compliance rules


def test_type_field_map_covers_all_types():
    pack = load_pack("construction")
    tfm = pack.type_field_map()
    assert set(tfm) == {
        "pile_test_report",
        "material_delivery_note",
        "inspection_report",
        "ncr_report",
        "invoice_payment_application",
        "safety_incident_report",
        "geotechnical_survey_report",
    }
    assert "pile_capacity" in tfm["pile_test_report"]


def test_compliance_evaluation_is_doc_type_scoped():
    pack = load_pack("construction")
    # A delivery note evaluated with delivery fields: pile rules do not apply,
    # so there are no failures from unrelated rules.
    report = compliance.evaluate_compliance(
        {"delivery_date": "2026-01-01", "quantity": "10"}, pack,
        doc_type="material_delivery_note",
    )
    assert report.overall == compliance.NA  # no rules for this type
    # The same fields with no doc_type run every rule (back-compat) -> NA.
    report_all = compliance.evaluate_compliance({"quantity": "10"}, pack)
    assert report_all.overall == compliance.NA


@pytest.mark.parametrize("name", sorted(EXPECTED_PACKS))
def test_every_document_type_declares_fields(name):
    pack = load_pack(name)
    assert pack.document_types, f"{name} should declare document types"
    for dt in pack.document_types:
        assert dt.fields, f"{name}/{dt.name} should declare its own fields"


def test_resolve_pack_falls_back_when_item_missing():
    moto = pytest.importorskip("moto")
    import boto3

    with moto.mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="packs",
            AttributeDefinitions=[{"AttributeName": "pack_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pack_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Item absent -> resolve_pack degrades to the bundled seed, no error.
        pack = resolve_pack("construction", table_name="packs", client=ddb)
        assert pack.industry == "construction"


def test_pack_fingerprint_stable_across_yaml_and_dynamodb_json():
    from_yaml = load_pack("construction")
    from_item = pack_from_item(
        {"pack_name": "construction", "definition": json.dumps(load_pack_data("construction"))}
    )
    assert from_yaml.content_hash.startswith("sha256:")
    assert len(from_yaml.content_hash) == len("sha256:") + 16
    assert from_yaml.content_hash == from_item.content_hash


def test_pack_fingerprint_changes_when_a_rule_changes():
    from idp_common.packs import pack_fingerprint

    data = load_pack_data("construction")
    before = pack_fingerprint(data)
    rule = data["document_types"][0]["compliance_rules"][0]
    rule["expr"] = rule["expr"] + " or 1 >= 0"
    assert pack_fingerprint(data) != before


def test_sink_row_keys_unchanged_by_pack_fingerprint():
    # The Iceberg schema must not change: the fingerprint rides inside the
    # existing compliance_result column, not a new one.
    from idp_common.record import flatten_for_sink

    row = flatten_for_sink(
        {"doc_id": "d", "compliance_result": "PASS [pack=construction@sha256:0]",
         "pack_content_hash": "sha256:0"}
    )
    assert set(row) == {
        "doc_id", "doc_type", "source_uri", "content_hash", "ingested_at",
        "mean_confidence", "field_count", "free_field_count", "paid_field_count",
        "method_summary_json", "fields_json", "field_provenance_json",
        "compliance_result", "compression_algo",
    }
