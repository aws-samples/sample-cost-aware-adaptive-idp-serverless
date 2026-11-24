# Interactive Demo (No AWS Required)

A local Streamlit dashboard that demonstrates the **cost-aware adaptive IDP**
pattern without deploying any infrastructure. Everything runs in-process using
the real YAML pack definitions and cost model from this repository.

## What It Shows

| Tab | Purpose |
|-----|---------|
| **How It Works** | Visual explanation of the tiered cost cascade and learning curve. |
| **Process Documents** | Simulate processing individual or batched documents through the cascade. |
| **Spatial Memory** | Inspect the learned templates and their confidence level. |
| **Cost Calculator** | Model blended cost for any document-mix and compare to the naive baseline. |
| **Industry Packs** | Browse the declarative YAML pack definitions (fields, rules, linking keys). |
| **Processing History** | Timeline of all processed documents with per-doc cost and a learning-curve chart. |

## Prerequisites

- Python 3.10+
- No AWS account or credentials needed

## Install & Run

```bash
cd sample-cost-aware-adaptive-idp-serverless   # repo root
pip install -r requirements-demo.txt
streamlit run demo_ui.py --server.address 127.0.0.1
```

The app opens at `http://localhost:8501`.

## Demo Walkthrough Script

Follow these steps in order to tell the **cost-savings story** in ~3 minutes:

### 1. Set the stage (Tab 1 — "How It Works")

- Open the **How It Works** tab (loaded by default).
- Point out the bar chart: Basic OCR + Spatial Memory costs **$1.67/1k docs**
  while Structured Textract costs the dynamically-computed naive baseline.
- Walk through the flow diagram: "Start cheap, escalate only when unclear,
  learn so repeats are free."

### 2. First document — escalation (Tab 2 — "Process Documents")

- Switch to the **Process Documents** tab.
- Select **energy** → **well_test_report**.
- Click **🚀 Process Document**.
- Observe: the tier is `llm` (first-time extraction) — the system has never
  seen this layout. Note the cost (~$0.002/doc) and reason "New layout".

### 3. Build confidence — second and third observations

- Click **🚀 Process Document** again (with "Add random variation" checked).
- Repeat once more (3 total observations).
- After the 3rd click, the spatial memory has now reached confidence.

### 4. Free reuse — fourth observation (Tab 2 → Tab 3)

- Click **🚀 Process Document** a 4th time (variation checked).
- Observe: tier is now `low_cost_ocr` / **"spatial_memory (free reuse)"**.
  The cost drops to ~$0.0017/doc — no AI needed.
- Switch to the **Spatial Memory** tab to see the green "Confident (free reuse)"
  badge and the 4 observations learned.

### 5. Generalize the savings (Tab 4 — "Cost Calculator")

- Switch to the **Cost Calculator** tab.
- Leave the default mix (70% OCR, 20% LLM, 8% BDA, 2% Structured).
- Point out the blended cost vs naive baseline metric and the savings percentage.
- Drag the "Basic OCR + Spatial" slider to 90% to show what happens as the
  system learns more layouts over time — savings increase further.

### 6. Show the learning curve (Tab 6 — "Processing History")

- Switch to the **Processing History** tab.
- The "Cost Per Document Over Time" chart shows the per-doc cost starting high
  and dropping below the naive baseline (red dashed line) as the spatial memory
  kicks in.
- Highlight: "The more you run, the cheaper it gets. After the first few
  documents of each type, processing is essentially free."

### 7. (Optional) Batch simulation

- Return to **Process Documents**, pick any doc type, run a **Batch Simulation**
  of 30 docs.
- Show the "Savings vs naive baseline" percentage. In this local **simulation**
  — a 30-document batch priced with the indicative us-east-1 list prices in
  `tools/cost_model.py` — it reaches ~90%+ because most of the batch resolves
  via spatial memory after the initial learning docs. This is a simulated
  figure for the chosen assumptions, not a measured or guaranteed real-world
  savings rate.

---

## Notes

- All costs are indicative AWS list prices (us-east-1). See `tools/cost_model.py`
  for details.
- Sample document fields are drawn from the actual YAML pack definitions in
  `src/common_layer/python/idp_common/pack_defs/`.
- No network calls are made; everything is simulated locally.
- The bundled `.streamlit/config.toml` turns Streamlit usage statistics off and
  binds the server to 127.0.0.1 only, so the demo is not reachable from other
  machines.
