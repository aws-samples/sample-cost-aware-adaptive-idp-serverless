# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Cost-Aware Adaptive IDP — Interactive Demo UI
==============================================

A Streamlit dashboard that demonstrates the cost-aware adaptive IDP pattern
WITHOUT requiring AWS deployment. Shows:
- How the tiered cost cascade works
- Spatial memory learning (learn once, reuse free)
- Industry pack configuration (YAML-driven)
- Cost savings calculator
- Document processing simulation

Usage:
    cd sample-cost-aware-adaptive-idp-serverless
    pip install -r requirements-demo.txt
    streamlit run demo_ui.py --server.address 127.0.0.1
"""

import hashlib
import json
import secrets
import sys
import os
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

# --- Page Config (must be the first Streamlit command) ---
st.set_page_config(
    page_title="Cost-Aware Adaptive IDP Demo",
    page_icon="💰",
    layout="wide",
)

# --- Robust imports from project modules ---
_REPO_ROOT = Path(__file__).resolve().parent
_COMMON_LAYER = _REPO_ROOT / "src" / "common_layer" / "python"
_PACK_DIR = _COMMON_LAYER / "idp_common" / "pack_defs"

try:
    if not _COMMON_LAYER.exists():
        raise FileNotFoundError(
            f"Common layer not found at: {_COMMON_LAYER}\n"
            "Please run this app from the repository root."
        )
    if not _PACK_DIR.exists():
        raise FileNotFoundError(
            f"Pack definitions directory not found at: {_PACK_DIR}\n"
            "Please run this app from the repository root."
        )
    sys.path.insert(0, str(_COMMON_LAYER))
    sys.path.insert(0, str(_REPO_ROOT))
    from idp_common.packs import load_pack, list_packs, IndustryPack
    from tools.cost_model import (
        CostInputs, compute, format_report, PRICES, BEDROCK_PRICES,
        tier_cost_per_doc,
    )
except (ImportError, FileNotFoundError) as exc:
    st.error(
        f"**Failed to load project modules.**\n\n"
        f"```\n{exc}\n```\n\n"
        f"**To fix:** run the demo from the repository root:\n"
        f"```bash\n"
        f"cd sample-cost-aware-adaptive-idp-serverless\n"
        f"pip install -r requirements-demo.txt\n"
        f"streamlit run demo_ui.py --server.address 127.0.0.1\n"
        f"```"
    )
    st.stop()

# --- Session State ---
if "spatial_memory" not in st.session_state:
    st.session_state.spatial_memory = {}  # template_id -> {fields, observations, doc_type}
if "processed_docs" not in st.session_state:
    st.session_state.processed_docs = []
if "dedup_hashes" not in st.session_state:
    st.session_state.dedup_hashes = set()


# =============================================================================
# Naive Baseline — single source of truth
# =============================================================================

def naive_baseline_per_1k(pages: float = 1.0, model: str = "nova-lite") -> float:
    """Compute the naive baseline cost per 1,000 docs dynamically."""
    return tier_cost_per_doc("structured", pages, model) * 1000


# =============================================================================
# Sample Documents — fields aligned to real YAML pack definitions
# =============================================================================

# Construction pack linking keys: project_id, asset_id, location, chainage
# Energy pack linking keys: well_id, asset_id, field_name, facility_code
# Healthcare pack linking keys: facility_id, batch_id, equipment_id, sample_id
#
# Each sample doc includes at least one linking key from its pack PLUS fields
# from that document type's declared field list in the YAML.

SAMPLE_DOCUMENTS = {
    "construction": {
        # Doc type: pile_test_report
        # Linking key used: project_id
        # Fields from YAML: pile_reference, pile_capacity, withstand_limit,
        #   concrete_strength, test_method, test_date, contractor, pile_diameter,
        #   pile_length, ...
        "pile_test_report": {
            "fields": {
                "project_id": "PRJ-2024-001",
                "asset_id": "PILE-SECTOR-A",
                "pile_reference": "P-14",
                "pile_capacity": "285",
                "withstand_limit": "250",
                "concrete_strength": "35",
                "test_date": "2024-03-15",
                "contractor": "Acme Piling Ltd",
                "pile_diameter": "600mm",
                "pile_length": "18m",
                "test_method": "Static load test",
            },
            "pages": 3,
        },
        # Doc type: material_delivery_note
        # Linking key used: project_id
        # Fields from YAML: delivery_date, material_type, quantity,
        #   unit_of_measure, supplier, po_number, batch_reference, ...
        "material_delivery_note": {
            "fields": {
                "project_id": "PRJ-2024-001",
                "location": "Zone B - Pad 3",
                "delivery_date": "2024-03-12",
                "material_type": "C40 Concrete",
                "quantity": "18",
                "unit_of_measure": "m³",
                "supplier": "Ready Mix Corp",
                "po_number": "PO-4521",
                "batch_reference": "BRF-2024-1187",
            },
            "pages": 1,
        },
        # Doc type: ncr_report
        # Linking key used: project_id
        # Fields from YAML: ncr_reference, date_raised, discipline, priority,
        #   defect_location, contractor, status, root_cause, ...
        "ncr_report": {
            "fields": {
                "project_id": "PRJ-2024-001",
                "chainage": "CH 1+250",
                "ncr_reference": "NCR-CWR-2024-0847",
                "date_raised": "2024-03-18",
                "discipline": "Structural",
                "priority": "HIGH",
                "defect_location": "Pile cap PC-07, east face",
                "contractor": "SubCon Services",
                "status": "Open",
                "root_cause": "Insufficient cover thickness",
            },
            "pages": 2,
        },
    },
    "energy": {
        # Doc type: well_test_report
        # Linking key used: well_id
        # Fields from YAML: well_id, flow_rate, operating_pressure,
        #   pressure_rating, choke_size, test_date, operator, fluid_type,
        #   water_cut_pct, ...
        "well_test_report": {
            "fields": {
                "well_id": "API-17-033-12345",
                "field_name": "Eagle Ford South",
                "flow_rate": "1250",
                "operating_pressure": "3200",
                "pressure_rating": "5000",
                "choke_size": "24/64",
                "test_date": "2024-06-15",
                "test_duration_hours": "72",
                "operator": "Eagle Energy",
                "fluid_type": "Oil",
                "water_cut_pct": "12",
            },
            "pages": 4,
        },
        # Doc type: pipeline_inspection
        # Linking key used: asset_id
        # Fields from YAML: asset_id, segment_id, wall_thickness,
        #   min_wall_thickness, corrosion_rate, inspection_date,
        #   inspection_method, anomaly_count, ...
        "pipeline_inspection": {
            "fields": {
                "asset_id": "PL-GULF-0042",
                "facility_code": "FAC-GOM-007",
                "segment_id": "SEG-14",
                "wall_thickness": "8.2",
                "min_wall_thickness": "6.5",
                "corrosion_rate": "0.15",
                "inspection_date": "2024-05-20",
                "inspection_method": "ILI (MFL)",
                "pipeline_diameter": "24 inch",
                "anomaly_count": "3",
            },
            "pages": 8,
        },
        # Doc type: maintenance_log
        # Linking key used: asset_id
        # Fields from YAML: asset_id, work_order, maintenance_type,
        #   technician, downtime_hours, completion_date, ...
        "maintenance_log": {
            "fields": {
                "asset_id": "COMP-T400-003",
                "facility_code": "FAC-GOM-007",
                "work_order": "WO-2024-08812",
                "maintenance_type": "Corrective",
                "technician": "J. Rodriguez",
                "downtime_hours": "6.5",
                "completion_date": "2024-07-02",
                "parts_replaced": "Seal kit, bearing assembly",
                "root_cause": "Vibration-induced fatigue",
            },
            "pages": 2,
        },
    },
    "healthcare": {
        # Doc type: lab_result_report
        # Linking key used: sample_id, batch_id
        # Fields from YAML: sample_id, batch_id, measured_value,
        #   specification_limit, test_method, result_date, analyst,
        #   unit_of_measure, product_name, lot_number, ...
        "lab_result_report": {
            "fields": {
                "sample_id": "SMP-2024-04821",
                "batch_id": "BATCH-XR-7790",
                "facility_id": "FAC-QC-EAST",
                "measured_value": "4.8",
                "specification_limit": "5.0",
                "test_method": "HPLC Assay",
                "result_date": "2024-04-10",
                "analyst": "M. Chen",
                "unit_of_measure": "mg/mL",
                "product_name": "Amoxicillin 500mg Capsules",
                "lot_number": "LOT-2024-3301",
            },
            "pages": 2,
        },
        # Doc type: equipment_calibration_log
        # Linking key used: equipment_id
        # Fields from YAML: equipment_id, calibration_due_days,
        #   last_calibration_date, technician, calibration_status, ...
        "equipment_calibration_log": {
            "fields": {
                "equipment_id": "EQ-HPLC-014",
                "facility_id": "FAC-QC-EAST",
                "calibration_due_days": "45",
                "last_calibration_date": "2024-06-01",
                "technician": "R. Patel",
                "calibration_status": "Pass",
                "equipment_model": "Agilent 1260 Infinity II",
                "serial_number": "DEABC12345",
                "next_due_date": "2024-09-01",
            },
            "pages": 1,
        },
        # Doc type: inspection_observation
        # Linking key used: facility_id
        # Fields from YAML: facility_id, observation_id, severity,
        #   inspector, status, observation_date, regulation_reference, ...
        "inspection_observation": {
            "fields": {
                "facility_id": "FAC-QC-EAST",
                "observation_id": "OBS-2024-003",
                "severity": "Major",
                "inspector": "FDA District Inspector",
                "status": "Open",
                "observation_date": "2024-05-15",
                "regulation_reference": "21 CFR 211.68",
                "corrective_action": "Recalibrate and revalidate equipment",
                "due_date": "2024-06-15",
            },
            "pages": 3,
        },
    },
}


def simulate_processing(doc_type: str, fields: dict, pages: int, industry: str) -> dict:
    """Simulate the cost-aware processing cascade."""

    content = json.dumps(fields, sort_keys=True)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    # Step 1: Dedup check
    if content_hash in st.session_state.dedup_hashes:
        return {
            "doc_type": doc_type,
            "tier": "dedup",
            "method": "skipped (duplicate)",
            "cost": 0.0,
            "fields_extracted": 0,
            "spatial_matched": False,
            "reason": f"Content hash {content_hash} already processed",
        }

    st.session_state.dedup_hashes.add(content_hash)

    # Step 2: Check spatial memory
    template_key = f"{industry}:{doc_type}"
    spatial_match = template_key in st.session_state.spatial_memory
    observations = st.session_state.spatial_memory.get(template_key, {}).get("observations", 0)

    # Step 3: Decide tier
    if spatial_match and observations >= 3:
        # Spatial memory is confident — resolve for free (just OCR cost)
        tier = "low_cost_ocr"
        method = "spatial_memory (free reuse)"
        reason = f"Template '{template_key}' matched with {observations} prior observations"
    elif spatial_match and observations < 3:
        # Partially learned — use LLM to refine
        tier = "llm"
        method = "llm (learning)"
        reason = f"Template has only {observations} observations, needs more confidence"
    else:
        # First time seeing this layout — escalate
        if pages > 3:
            tier = "bda"
            method = "bda (first-time visual)"
            reason = "New layout with multiple pages/charts — BDA needed"
        else:
            tier = "llm"
            method = "llm (first-time extraction)"
            reason = "New layout — LLM extracting fields and training spatial memory"

    # Step 4: Learn into spatial memory
    if template_key not in st.session_state.spatial_memory:
        st.session_state.spatial_memory[template_key] = {
            "fields": list(fields.keys()),
            "observations": 1,
            "doc_type": doc_type,
            "first_seen": len(st.session_state.processed_docs),
        }
    else:
        st.session_state.spatial_memory[template_key]["observations"] += 1

    # Step 5: Compute cost
    cost = tier_cost_per_doc(tier, pages, "nova-lite")

    result = {
        "doc_type": doc_type,
        "tier": tier,
        "method": method,
        "cost": cost,
        "cost_per_1k": cost * 1000,
        "fields_extracted": len(fields),
        "spatial_matched": spatial_match,
        "reason": reason,
        "content_hash": content_hash,
        "pages": pages,
        "fields": fields,
    }

    st.session_state.processed_docs.append(result)
    return result


# =============================================================================
# UI
# =============================================================================

st.title("💰 Cost-Aware Adaptive IDP — Interactive Demo")
st.markdown("*Learn once, reuse for free. AI only when needed.*")

tabs = st.tabs([
    "How It Works",
    "Process Documents",
    "Spatial Memory",
    "Cost Calculator",
    "Industry Packs",
    "Processing History",
])


# --- Tab 1: How It Works ---
with tabs[0]:
    st.header("How the Cost Cascade Works")

    st.markdown("""
    ### The Problem
    Most IDP systems run expensive extraction (Textract FORMS at **$65/1,000 pages** 
    or LLMs at **$3-15/1M tokens**) on EVERY document — even when 70% of documents 
    follow a layout the system has already seen.
    
    ### The Solution: Tiered Cascade
    Start cheap. Only escalate when unclear. Learn layouts so repeats are free.
    """)

    # Cost comparison chart — all values derived from the cost model
    _baseline = naive_baseline_per_1k()
    tiers_display = ["Basic OCR +\nSpatial Memory", "Low-cost LLM\n(Nova Lite)", "Bedrock Data\nAutomation", "Structured\nTextract (naive)"]
    costs_display = [
        tier_cost_per_doc("low_cost_ocr", 1, "nova-lite") * 1000,
        tier_cost_per_doc("llm", 1, "nova-lite") * 1000,
        tier_cost_per_doc("bda", 1, "nova-lite") * 1000,
        _baseline,
    ]
    colors = ["#4CAF50", "#2196F3", "#FF9800", "#F44336"]

    fig = go.Figure(data=[go.Bar(
        x=tiers_display, y=costs_display,
        marker_color=colors,
        text=[f"${c:.2f}" for c in costs_display],
        textposition="auto",
    )])
    fig.update_layout(
        title="Cost per 1,000 Documents by Tier (1 page/doc, Nova Lite)",
        yaxis_title="$ per 1,000 docs",
        height=400,
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # Flow diagram
    st.markdown("""
    ### Processing Flow
    
    ```
    📄 Document Upload
         │
         ▼
    ┌─────────────────────────┐
    │  PREPROCESS (FREE)       │  Content hash dedup
    │  Duplicate? → SKIP       │  Never reprocess identical files
    └─────────────┬───────────┘
                  │ (new document)
                  ▼
    ┌─────────────────────────┐
    │  BASIC OCR ($1.50/1k pg) │  Textract DetectDocumentText
    │  + SPATIAL MEMORY (FREE) │  Check known templates
    └─────────────┬───────────┘
                  │
            ┌─────┴─────┐
            │           │
         CLEAR?      NOT CLEAR
            │           │
            ▼           ▼
    ┌──────────┐  ┌────────────────┐
    │  DONE!   │  │  ESCALATE:     │
    │  (cheap) │  │  • LLM ($0.30) │
    └──────────┘  │  • BDA ($40/pg)│
                  └───────┬────────┘
                          │
                          ▼
                  ┌──────────────┐
                  │ LEARN LAYOUT │  → Spatial Memory
                  │ (next time   │    (next same layout = FREE)
                  │  = FREE!)    │
                  └──────────────┘
    ```
    
    ### The Key Insight
    
    | Document # | Layout Seen Before? | Tier Used | Cost |
    |:---:|:---:|:---:|:---:|
    | 1st | No | LLM/BDA (paid) | ~$0.04 |
    | 2nd | Yes (1 obs) | LLM (verifying) | ~$0.002 |
    | 3rd | Yes (2 obs) | LLM (learning) | ~$0.002 |
    | 4th+ | Yes (3+ obs) | **Spatial only (FREE)** | **~$0.0017** |
    
    > After ~3 observations of a layout, all future documents of that type resolve 
    > at basic OCR cost only. **The more you run, the cheaper it gets.**
    """)


# --- Tab 2: Process Documents ---
with tabs[1]:
    st.header("Process a Document")
    st.markdown("Simulate processing a document through the cost-aware cascade.")

    col1, col2 = st.columns([1, 2])

    with col1:
        pack_dir = str(_PACK_DIR)
        available_packs = list_packs(pack_dir)
        selected_industry = st.selectbox("Industry Pack", available_packs)

        doc_types = list(SAMPLE_DOCUMENTS.get(selected_industry, {}).keys())
        if not doc_types:
            st.warning(f"No sample documents for '{selected_industry}'. Try construction or energy.")
            st.stop()

        selected_doc_type = st.selectbox("Document Type", doc_types)
        doc_data = SAMPLE_DOCUMENTS[selected_industry][selected_doc_type]

        # Add variation option
        add_variation = st.checkbox("Add random variation (simulate different instance)", value=False)

        if st.button("🚀 Process Document", type="primary"):
            fields = dict(doc_data["fields"])
            if add_variation:
                # Modify a field to make it a "different" document
                keys = list(fields.keys())
                fields[keys[-1]] = fields[keys[-1]] + f"_{secrets.randbelow(900) + 100}"

            result = simulate_processing(
                selected_doc_type, fields, doc_data["pages"], selected_industry
            )

            # Show result
            st.markdown("---")
            if result["tier"] == "dedup":
                st.warning(f"⏭️ **SKIPPED** — Duplicate document (hash: {result.get('content_hash', 'N/A')})")
            else:
                tier_colors = {
                    "low_cost_ocr": "🟢", "llm": "🔵", "bda": "🟠", "structured": "🔴"
                }
                icon = tier_colors.get(result["tier"], "⚪")
                st.success(f"{icon} **Tier: {result['tier']}** — {result['method']}")
                st.info(f"💡 Reason: {result['reason']}")

                col_a, col_b, col_c = st.columns(3)
                col_a.metric("Cost", f"${result['cost']:.5f}")
                col_b.metric("Per 1,000 docs", f"${result['cost_per_1k']:.2f}")
                col_c.metric("Fields Extracted", result["fields_extracted"])

        # Batch processing
        st.markdown("---")
        st.subheader("Batch Simulation")
        batch_size = st.slider("Documents to process", 5, 50, 20)

        if st.button("📦 Process Batch (same type)"):
            results = []
            for i in range(batch_size):
                fields = dict(doc_data["fields"])
                fields["batch_id"] = f"batch_{i+1:03d}"
                r = simulate_processing(
                    selected_doc_type, fields, doc_data["pages"], selected_industry
                )
                results.append(r)

            # Show batch summary
            tiers_used = {}
            total_cost = 0
            for r in results:
                tiers_used[r["tier"]] = tiers_used.get(r["tier"], 0) + 1
                total_cost += r["cost"]

            naive_per_doc = tier_cost_per_doc("structured", doc_data["pages"], "nova-lite")

            st.markdown("### Batch Results")
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Total Cost", f"${total_cost:.4f}")
            col_b.metric("Avg Cost/Doc", f"${total_cost/batch_size:.5f}")
            col_c.metric("Naive Cost Would Be", f"${naive_per_doc * batch_size:.4f}")

            st.markdown("**Tiers Used:**")
            for tier, count in tiers_used.items():
                pct = count / batch_size * 100
                st.markdown(f"- {tier}: {count} docs ({pct:.0f}%)")

            savings = 1 - (total_cost / (naive_per_doc * batch_size))
            st.success(f"**💰 Savings vs naive baseline: {savings*100:.1f}%**")

    with col2:
        st.subheader("Document Preview")
        if doc_types:
            doc_data_preview = SAMPLE_DOCUMENTS[selected_industry][selected_doc_type]
            st.markdown(f"**Type:** {selected_doc_type}")
            st.markdown(f"**Pages:** {doc_data_preview['pages']}")
            st.markdown("**Fields:**")
            st.json(doc_data_preview["fields"])


# --- Tab 3: Spatial Memory ---
with tabs[2]:
    st.header("Spatial Memory State")
    st.markdown("""
    Spatial memory learns document layouts. After enough observations, 
    documents of the same type resolve for **free** (just basic OCR).
    """)

    if not st.session_state.spatial_memory:
        st.info("No templates learned yet. Process some documents in the 'Process Documents' tab.")
    else:
        for template_id, data in st.session_state.spatial_memory.items():
            obs = data["observations"]
            status = "🟢 Confident (free reuse)" if obs >= 3 else "🟡 Learning" if obs >= 1 else "🔴 New"

            with st.expander(f"{status} **{template_id}** — {obs} observations", expanded=obs < 3):
                col1, col2, col3 = st.columns(3)
                col1.metric("Observations", obs)
                col2.metric("Fields Learned", len(data["fields"]))
                col3.metric("Status", "Confident" if obs >= 3 else "Learning")

                st.markdown("**Learned Fields:**")
                st.write(data["fields"])

                if obs >= 3:
                    st.success("✅ This template is now confident. Future documents of this type will resolve at basic OCR cost only — NO AI needed!")
                else:
                    remaining = 3 - obs
                    st.warning(f"⏳ Need {remaining} more observation(s) before this template becomes confident.")

        st.markdown("---")
        if st.button("🗑️ Reset Spatial Memory"):
            st.session_state.spatial_memory = {}
            st.session_state.processed_docs = []
            st.session_state.dedup_hashes = set()
            st.rerun()


# --- Tab 4: Cost Calculator ---
with tabs[3]:
    st.header("Cost Calculator")
    st.markdown("Model cost savings for your document population.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Document Mix")
        st.markdown("What percentage of your documents resolve at each tier?")
        ocr_pct = st.slider("Basic OCR + Spatial (cheap)", 0, 100, 70, key="calc_ocr")
        llm_pct = st.slider("LLM Escalation (low-cost)", 0, 100, 20, key="calc_llm")
        bda_pct = st.slider("BDA (visual/charts)", 0, 100, 8, key="calc_bda")
        structured_pct = st.slider("Structured Textract (expensive)", 0, 100, 2, key="calc_struct")

        total_pct = ocr_pct + llm_pct + bda_pct + structured_pct
        if total_pct != 100:
            st.warning(f"Mix sums to {total_pct}% (must be 100%)")

    with col2:
        st.subheader("Configuration")
        pages_per_doc = st.slider("Pages per document", 1, 20, 1)
        model = st.selectbox("LLM Model", list(BEDROCK_PRICES.keys()), index=0)
        num_docs = st.number_input("Documents to process", 100, 1000000, 10000, step=1000)

    if total_pct == 100:
        st.markdown("---")

        inputs = CostInputs(
            low_cost_ocr=ocr_pct / 100,
            llm=llm_pct / 100,
            bda=bda_pct / 100,
            structured=structured_pct / 100,
            pages=pages_per_doc,
            model=model,
            docs=num_docs,
        )
        report = compute(inputs)

        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Blended Cost / 1k docs", f"${report['blended_per_1000']:.2f}")
        col_b.metric("Naive Baseline / 1k docs", f"${report['naive_baseline_per_1000']:.2f}")
        col_c.metric("Savings", f"{report['savings_vs_naive_pct']}%")
        col_d.metric(f"Total ({num_docs:,} docs)", f"${report['blended_total']:.2f}")

        # Comparison chart
        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="Cost-Aware (This Pattern)",
            x=["Your Cost"], y=[report["blended_per_1000"]],
            marker_color="#4CAF50",
            text=[f"${report['blended_per_1000']:.2f}"],
            textposition="auto",
        ))
        fig.add_trace(go.Bar(
            name="Naive Baseline (Structured on Everything)",
            x=["Your Cost"], y=[report["naive_baseline_per_1000"]],
            marker_color="#F44336",
            text=[f"${report['naive_baseline_per_1000']:.2f}"],
            textposition="auto",
        ))
        fig.update_layout(
            title="Cost Comparison: $/1,000 Documents",
            yaxis_title="$",
            barmode="group",
            height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Per-tier breakdown
        st.subheader("Per-Tier Breakdown")
        tier_data = report["per_tier_per_1000"]
        fig2 = go.Figure(data=[go.Pie(
            labels=list(tier_data.keys()),
            values=[report["mix"][t] * tier_data[t] for t in tier_data],
            marker_colors=["#4CAF50", "#2196F3", "#FF9800", "#F44336"],
            textinfo="label+percent",
        )])
        fig2.update_layout(title="Cost Distribution by Tier", height=350)
        st.plotly_chart(fig2, use_container_width=True)


# --- Tab 5: Industry Packs ---
with tabs[4]:
    st.header("Industry Packs (YAML-Driven)")
    st.markdown("""
    The engine is **industry-agnostic**. All domain behavior is defined in declarative YAML packs.
    No code changes needed to add a new industry — just drop a YAML file.
    """)

    pack_dir = str(_PACK_DIR)
    packs = list_packs(pack_dir)

    for pack_name in packs:
        pack = load_pack(pack_name, pack_dir)
        with st.expander(f"📦 **{pack.industry.title()}** — {len(pack.document_types)} document types", expanded=True):
            col1, col2 = st.columns(2)

            with col1:
                st.markdown(f"**Linking Keys:** `{', '.join(pack.linking_keys)}`")
                st.markdown(f"**Document Types:**")
                for dt in pack.document_types:
                    st.markdown(f"- **{dt.name}** ({len(dt.fields)} fields, {len(dt.compliance_rules)} compliance rules)")

            with col2:
                st.markdown("**Value-at-Risk Weights:**")
                if pack.value_at_risk_weights:
                    for field_name, weight in pack.value_at_risk_weights.items():
                        st.markdown(f"- {field_name}: `{weight}`")
                else:
                    st.markdown("*(none defined)*")

                st.markdown("**Compliance Rules:**")
                rules = pack.compliance_rules
                if rules:
                    for rule in rules[:5]:
                        st.markdown(f"- `{rule.id}`: `{rule.expr}`")
                else:
                    st.markdown("*(none)*")

    st.markdown("---")
    st.subheader("Raw YAML (Construction Pack)")
    yaml_path = os.path.join(pack_dir, "construction.yaml")
    with open(yaml_path, "r") as f:
        st.code(f.read(), language="yaml")


# --- Tab 6: Processing History ---
with tabs[5]:
    st.header("Processing History")

    if not st.session_state.processed_docs:
        st.info("No documents processed yet. Go to 'Process Documents' tab.")
    else:
        # Summary metrics
        total_docs = len(st.session_state.processed_docs)
        total_cost = sum(d["cost"] for d in st.session_state.processed_docs)
        naive_cost = sum(
            tier_cost_per_doc("structured", d.get("pages", 1), "nova-lite")
            for d in st.session_state.processed_docs if d["tier"] != "dedup"
        )

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Documents Processed", total_docs)
        col2.metric("Total Cost", f"${total_cost:.4f}")
        col3.metric("Naive Would Cost", f"${naive_cost:.4f}")
        savings_pct = (1 - total_cost / naive_cost) * 100 if naive_cost > 0 else 0
        col4.metric("Savings", f"{savings_pct:.1f}%")

        st.markdown("---")

        # Tier distribution
        tier_counts = {}
        for d in st.session_state.processed_docs:
            tier_counts[d["tier"]] = tier_counts.get(d["tier"], 0) + 1

        fig = go.Figure(data=[go.Pie(
            labels=list(tier_counts.keys()),
            values=list(tier_counts.values()),
            marker_colors=["#4CAF50", "#2196F3", "#FF9800", "#F44336", "#9E9E9E"],
        )])
        fig.update_layout(title="Documents by Tier", height=300)
        st.plotly_chart(fig, use_container_width=True)

        # Cost over time (learning curve)
        if len(st.session_state.processed_docs) > 1:
            st.subheader("Cost Per Document Over Time (Learning Curve)")
            costs_over_time = [d["cost"] * 1000 for d in st.session_state.processed_docs]

            # Compute a representative naive baseline for the chart.
            # Use the average pages across processed docs to keep the line meaningful.
            avg_pages = sum(d.get("pages", 1) for d in st.session_state.processed_docs) / total_docs
            _chart_baseline = naive_baseline_per_1k(avg_pages)

            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                y=costs_over_time,
                mode="lines+markers",
                name="Actual Cost ($/1k)",
                line=dict(color="#4CAF50"),
            ))
            fig2.add_hline(
                y=_chart_baseline, line_dash="dash", line_color="red",
                annotation_text=f"Naive baseline (${_chart_baseline:.2f}/1k)",
            )
            fig2.update_layout(
                xaxis_title="Document #",
                yaxis_title="$ per 1,000 docs equivalent",
                height=350,
            )
            st.plotly_chart(fig2, use_container_width=True)

        # History table
        st.subheader("All Processed Documents")
        history_data = []
        for i, d in enumerate(st.session_state.processed_docs):
            history_data.append({
                "#": i + 1,
                "Type": d["doc_type"],
                "Tier": d["tier"],
                "Method": d["method"],
                "Cost": f"${d['cost']:.5f}",
                "Fields": d["fields_extracted"],
                "Spatial Match": "✓" if d["spatial_matched"] else "✗",
            })
        st.dataframe(history_data, use_container_width=True, hide_index=True)

        st.markdown("---")
        if st.button("🗑️ Clear History"):
            st.session_state.processed_docs = []
            st.session_state.spatial_memory = {}
            st.session_state.dedup_hashes = set()
            st.rerun()
