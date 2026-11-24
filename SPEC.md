# sample-cost-aware-adaptive-idp-serverless — Design Specification

| | |
|---|---|
| **Status** | Design of record (aligned with the current implementation) |
| **Type** | AWS sample / reference pattern (open source, MIT-0, `aws-samples` target) |
| **Deployment** | AWS-native, fully serverless. IaC: AWS CDK (Python 3.14). Deployed by the user in their own account. |
| **Tagline** | Cost-aware, adaptive Intelligent Document Processing (IDP) — *"AI only when needed."* |

---

## 1. Overview

A serverless, industry-agnostic Intelligent Document Processing (IDP) pattern
that minimizes AI/ML spend by resolving most documents with **low-cost OCR** and
**free, deterministic spatial memory**, and escalating to **paid AI** (a low-cost
LLM, or a managed visual extraction service) only when a document is not clear.
Every extracted field records the tier/method that produced it, so cost savings
are **provable in SQL**, not merely asserted.

The engine is **industry-agnostic**. Per-industry behavior is supplied by
declarative **industry packs** (YAML). Any industry can be added as a pack with
no engine code changes; the repository ships several example packs.

The pattern **complements** a full end-to-end extractor (for example, the AWS
GenAI IDP Accelerator): it can act as a cost-reducing pre-filter and is **not** a
competing end-to-end extractor.

---

## 2. Goals and non-goals

### Goals
- Minimize per-document processing cost via a tiered cascade (the lowest-cost
  confident tier wins).
- Use paid AI only when deterministic methods are not confident.
- Be fully serverless and pay-as-you-go (scale to zero; no standing charges other
  than stored data).
- Make cost savings measurable (per-field provenance + Athena queries).
- Be industry-agnostic via declarative packs.
- Store structured results in an open table format (Apache Iceberg via Amazon S3
  Tables).
- Provide low-cost, audit-ready long-term retention of source documents.

### Non-goals (out of scope)
- A full end-to-end IDP that competes with a dedicated extraction accelerator.
- CAD / BIM / geospatial intelligence.
- Full document-image understanding beyond field/value extraction.
- Production hardening (queue buffering, multi-account, HA) — this is a reference
  sample to be adapted, not production software.

---

## 3. Positioning and differentiators

1. **Spatial-first cost minimization** — *AI only when needed*; escalation tiers
   train spatial memory so repeat layouts stop escalating.
2. **Cross-document reconciliation** — link records across a project/asset/entity
   lifecycle and check consistency using the active pack.
3. **Open-table provenance** — Apache Iceberg records (S3 Tables) with per-field
   cost provenance; optional S3 object annotations.
4. **Configurable tiered archival** — low-cost, audit-ready source retention.

The generic IDP space is crowded, so the README includes a "How this differs
from / complements the AWS GenAI IDP Accelerator" section.

---

## 4. Requirements

### Functional
- **FR1** Ingest documents from S3 and trigger processing automatically.
- **FR2** Preprocess (free): deduplicate by content hash and route; perform no
  extraction and no AI.
- **FR3** Run low-cost OCR (`DetectDocumentText`) on every non-duplicate document
  and classify it against learned spatial templates.
- **FR4** Escalate only when the OCR result is not clear: text-heavy documents to
  a low-cost LLM; images/charts/visually-complex documents to a managed visual
  extraction service.
- **FR5** Spatial memory self-learns field names + positions; no predefined
  schema is required.
- **FR6** Escalation tiers train spatial memory (record field positions) so
  future documents of the same layout resolve on the low-cost path.
- **FR7** Merge results into one record per document with **per-field provenance**
  (field → method/tier/confidence/bounding box).
- **FR8** Reconcile across documents and run compliance checks using the active
  industry pack.
- **FR9** Sink records to Apache Iceberg (S3 Tables) with cost/provenance columns.
- **FR10** Optionally attach object annotations to source objects (toggle, off by
  default).
- **FR11** Lifecycle source documents to low-cost storage or expiry (configurable);
  support transparent audit retrieval.
- **FR12** Provide a cost calculator producing $/1,000 docs per tier + blended +
  storage + naive baseline.

### Non-functional
- **NFR1** Fully serverless; no standing charges other than stored data.
- **NFR2** Per-document isolation (Lambda).
- **NFR3** Encryption at rest (SSE-KMS) and least-privilege IAM per function.
- **NFR4** Industry behavior is config-driven (no code change to add a pack).
- **NFR5** Unit-testable without an AWS account (`moto` + mocked Textract/Bedrock).
- **NFR6** Cost savings observable via Athena; operations observable via CloudWatch.

---

## 5. Architecture

```
S3 input bucket ──(EventBridge: Object Created)──▶ Step Functions (orchestrator)
  1. Preprocess (free)      ── dedup by content hash + routing (DynamoDB dedup)
        │  duplicate? ── yes ──▶ skip
        │  no
  2. Textract DetectDocumentText (basic OCR, default) + spatial memory (DynamoDB)
        │  clear? ── yes ──▶ Spatial ──▶ Reconcile ──▶ Sink
        │  no
        ├─ image/chart/hard ──▶ Bedrock Data Automation (durable) ──▶ learn ──▶ Reconcile ──▶ Sink
        └─ text            ──▶ [structured Textract, opt-in] ──▶ low-cost LLM (Nova) ──▶ learn ──▶ Reconcile ──▶ Sink
  N. Sink ──▶ Amazon Data Firehose ──▶ S3 Tables (managed Apache Iceberg)

Source lifecycle:  Glacier IR transition (retain) OR source expiry — configurable
Audit retrieval (opt-in): S3 Object Lambda Access Point (transparent decompress on GET)
Analytics:         Amazon Athena over S3 Tables (cost-savings queries)
Observability:     CloudWatch dashboard + alarms (workflow + Firehose delivery)
```

The async Textract and Bedrock Data Automation tiers run as **Lambda durable
functions** invoked with the Step Functions task-token pattern: they `wait`
between status checks (suspending with no compute charge) and report back on
completion. Step Functions coordinates across services; durable functions own
the long async wait.

### Service mapping

| Concern | AWS service |
|---|---|
| Ingestion + source store | Amazon S3 + Amazon EventBridge |
| Orchestration | AWS Step Functions (Standard) |
| Compute | AWS Lambda (Python 3.14); durable functions for async Textract + BDA |
| OCR (default) | Amazon Textract `DetectDocumentText` |
| Structured OCR (opt-in) | Amazon Textract `AnalyzeDocument` FORMS+TABLES |
| Text AI escalation | Amazon Bedrock (Converse API — Nova / Claude, configurable) |
| Visual AI escalation | Amazon Bedrock Data Automation (managed; fields + bounding boxes) |
| Spatial template store, dedup, industry packs | Amazon DynamoDB (on-demand) |
| Open-table sink | Amazon Data Firehose → Amazon S3 Tables (managed Apache Iceberg) |
| Catalog + query | AWS Glue Data Catalog + Amazon Athena |
| Audit retrieval (optional, opt-in) | Amazon S3 Object Lambda Access Point |
| Object context (optional) | S3 object tagging (opt-in adapter) |
| Archival | S3 Lifecycle (configurable: Glacier IR transition, or source expiry) |
| Security | SSE-KMS, least-privilege IAM per function |
| Observability | Amazon CloudWatch (dashboard + alarms), AWS X-Ray |
| Config | Industry packs (DynamoDB, seeded from bundled YAML; runtime-editable) + CDK context (`cdk.json`) |

---

## 6. Cost cascade (the core mechanism)

Every non-duplicate document runs basic OCR first; escalation tiers add their
service cost on top of that single OCR pass. Escalate only on "not clear."

| Tier | Relative cost | When it runs | Mechanism |
|---|---|---|---|
| **Preprocess** | Free | Every upload | Content-hash dedup + routing. No extraction. |
| **Basic OCR + spatial** | Low | Every new document (default) | `DetectDocumentText` + free spatial template memory. |
| **Low-cost LLM (text)** | Low | OCR text not clear | Bedrock Converse (Nova/Claude). Trains spatial memory from the OCR layout. |
| **Bedrock Data Automation (visual)** | Medium | OCR not clear + image/chart/hard | Managed extraction; returns fields with bounding boxes that train spatial memory. |
| **Structured Textract** | High | Opt-in only | `AnalyzeDocument` FORMS+TABLES; off by default. |

### Indicative list prices (us-east-1; verify before quoting)
- Textract `DetectDocumentText` ≈ $1.50 / 1,000 pages; `AnalyzeDocument`
  FORMS+TABLES ≈ $65 / 1,000 pages.
- Bedrock Data Automation (custom output) ≈ $40 / 1,000 pages.
- Bedrock Nova Lite ≈ $0.06 in / $0.24 out per 1M tokens; Claude Haiku ≈
  $0.25 / $1.25; Claude Sonnet ≈ $3 / $15.
- Step Functions Standard ≈ $0.025 / 1,000 transitions; Lambda ≈ $0.20 / 1M req +
  $0.0000166667 / GB-s.
- Firehose ≈ per-GB ingested (5 KB-increment billing), no fixed fee. Athena ≈
  $5 / TB scanned.

### Indicative blended cost
With a representative mix (most documents resolved on basic OCR + spatial, a
minority escalating to LLM/BDA, structured Textract off), the modeled blended
cost is **~$6 / 1,000 documents** versus **~$67 / 1,000** for a naive "structured
extraction on every document" baseline — an ~90% reduction. `tools/cost_model.py`
computes this and is fully editable.

---

## 7. Processing model

- **Document-level cascade.** Each document flows through the cascade above; the
  merged result is **one record per document**.
- **Per-field provenance.** The record carries a `field_provenance` map:
  `field → { method, confidence, region_bbox }`. `method` ∈
  `{ spatial, textract, textract_custom, table, llm, bda }`. Only `spatial` is
  free; the rest are paid managed services.
- **Escalations train the free tier.** The LLM tier learns from the OCR layout
  (which carries bounding boxes); the BDA tier learns from its own visual
  grounding boxes. Both write into spatial memory.

---

## 8. Spatial memory

- **Self-learning** — no predefined fields. On `record`, each detected key
  becomes a field name and its bounding box is stored; on later documents,
  positions are refined by decayed weighted averaging, and occurrence counts
  track maturity.
- **Source-agnostic** — learns from Textract OCR word boxes and from BDA visual
  grounding boxes alike.
- **Cold-start** — the first documents of a new layout escalate (paid, once) and
  create the template; blended cost per document decreases as templates mature.
- **Store** — small JSON templates in DynamoDB (on-demand); a local file store is
  used for tests. Reads are cached in Lambda memory; writes use conditional
  updates for concurrency.
- The engine is an independent implementation; its core idea is credited to the
  open-source `aws-samples/sample-textract-field-memory` (see `NOTICE`). No code
  was ported from it.

---

## 9. Industry packs (the domain layer)

Declarative YAML per industry. Spatial extraction works without packs; packs give
the learned fields *meaning*. A pack defines:
- **Document types, each with its own `fields` and `compliance_rules`** — fields
  and rules are per document type, not shared across the industry. A field entry
  is a bare name or `{name, description}`; the optional description (a hint,
  synonym, or unit) sharpens the LLM prompt line and the BDA blueprint
  instruction for that field.
- **Linking keys** (pack-level) for reconciliation across a project/asset/entity
  lifecycle.
- **Value-at-risk weights** per field (feed the escalation decision).

The LLM tier classifies a document into one of the pack's types and extracts that
type's fields (plus any other labeled field it finds); reconcile evaluates only
that type's rules. The pack also derives the **Bedrock Data Automation
blueprints — one per document type** (BDA matches each document to the
best-fitting blueprint, up to 40 per project, and reports the match, giving both
extraction and classification). Every blueprint additionally declares a small
set of inferred cross-industry catch-all fields (document date, reference
number, issuing organization, prepared by) since BDA, unlike the LLM prompt, has
no "extract anything else" fallback. Additional packs and document types are
added without engine code changes.

Packs are stored as data in a DynamoDB table (seeded create-only from the bundled
YAML) and read at runtime by `reconcile` and the LLM tier, so fields, compliance
rules, and document types can be added to an industry without a redeploy and
without CloudFormation drift. The BDA blueprint is derived from the pack at deploy
time, so managed-visual-tier field changes take effect on the next pipeline deploy.

---

## 10. Storage, lifecycle and audit

- **Source documents (configurable):**
  - Glacier enabled (default): transition to Glacier Instant Retrieval after
    `glacierTransitionDays` and retain for long-term audit.
  - Glacier disabled: expire source documents after `sourceExpiryDays` (the
    structured record is already in Iceberg, so the raw source is disposable).
- **Compression (optional):** the algorithm and original filename are recorded in
  provenance to drive transparent retrieval.
- **Audit retrieval (opt-in, `idp:retrievalEnabled`):** an S3 Object Lambda
  Access Point transparently decompresses on GET. Off by default because, since
  7 November 2025, S3 Object Lambda is open only to accounts that already use it.
- **Encryption:** SSE-KMS throughout (customer-managed key).
- **Retention windows are configurable:** CloudWatch Logs, Firehose error records,
  BDA scratch output, and Athena results all default to bounded windows to avoid
  unbounded storage growth.

---

## 11. Open-table sink and analytics

- **Sink:** the final Lambda emits the merged record to Amazon Data Firehose,
  which appends to a managed Apache Iceberg table in S3 Tables. No custom Iceberg
  writer; Firehose handles buffering and retries.
- **Schema:** a fixed 14-column table. Scalar cost columns
  (`field_count`, `free_field_count`, `paid_field_count`, `mean_confidence`,
  `method_summary_json`) are first-class; variable-length extracted data lives in
  JSON string columns (`fields_json`, `field_provenance_json`). Failed deliveries
  land in an error bucket for replay.
- **Schema evolution:** the table's `IcebergMetadata` is replace-on-update in
  CloudFormation, so add columns on a live table via the engine
  (`ALTER TABLE ... ADD COLUMNS`) rather than by editing the CDK schema. Because
  fields are stored in `fields_json`, column changes are rarely needed.
- **Analytics:** Athena over S3 Tables; saved cost-savings queries compute the
  free-vs-paid field mix and the share of documents that never used AI.

---

## 12. Object annotations (optional, default OFF)

- Toggleable adapter; a strict no-op when disabled.
- When enabled, attaches object-level context (doc type, compliance, linking
  keys, extraction id) to each source object via **S3 object tagging** — a
  portable stand-in for native S3 metadata/annotations, which can replace the
  single tagging call where available.
- Best-effort and non-fatal: a tagging failure never fails the pipeline. Subject
  to S3 object-tagging limits (10 tags/object; key/value length caps).

---

## 13. Serverless orchestration

- **Step Functions Standard** coordinates the flow across services.
- **Lambda durable functions** handle the async Textract and BDA tiers via the
  task-token pattern (suspend with no compute charge during the wait).
- **EventBridge** triggers executions on S3 upload.
- All functions are zip-based (no container images).

---

## 14. Security and IAM

- Least-privilege execution role per function (scoped to its S3 prefixes,
  DynamoDB tables, Bedrock models, and Textract/BDA APIs; a few runtime actions
  and task-token callbacks are not resource-addressable and use `"*"` — scope
  further for production).
- SSE-KMS on S3 buckets and DynamoDB.
- No public endpoint; the pipeline is triggered by S3 uploads via EventBridge.
- Treat all document content as untrusted; keep PII out of logs.

---

## 15. Cost model tool

`tools/cost_model.py` — inputs: document mix, average pages/doc, Bedrock model,
retain-source and annotations toggles. Outputs: $/1,000 docs per tier, blended
cost, storage cost, and the naive-baseline comparison. Uses the indicative list
prices above (clearly labeled and editable).

---

## 16. Repository structure

```
sample-cost-aware-adaptive-idp-serverless/
├── SPEC.md  README.md  LICENSE  NOTICE  CONTRIBUTING.md  CODE_OF_CONDUCT.md
├── app.py  cdk.json  requirements*.txt
├── infra/                       # CDK stacks
│   ├── config.py                # context-driven AppConfig (all knobs)
│   ├── storage_stack.py         # S3 input + lifecycle, BDA output bucket, DynamoDB, S3 Tables, KMS
│   ├── sink_stack.py            # Firehose → S3 Tables (Iceberg) + table + error bucket
│   ├── pipeline_stack.py        # Lambdas + Step Functions + EventBridge + BDA project/blueprint
│   ├── retrieval_stack.py       # S3 Object Lambda access point (decompress, opt-in)
│   └── observability_stack.py   # Athena workgroup + queries + dashboard + alarms
├── src/
│   ├── common_layer/python/idp_common/   # shared engine
│   │   ├── models.py  template_memory/  text_lines.py  pdf_utils.py
│   │   ├── textract.py  bedrock.py  bda.py  escalation.py  record.py
│   │   ├── packs.py  pack_defs/*.yaml  compliance.py  compression.py  annotations.py
│   ├── deps_layer/              # third-party deps layer (pure-Python; built by script)
│   └── handlers/                # preprocess, spatial, textract, extract_fallback (Nova),
│                                # bda, reconcile, sink, retrieval
├── analytics/queries/*.sql       # Athena cost-savings queries
├── tools/cost_model.py           # $/1,000 docs calculator
├── scripts/build_deps_layer.sh   # builds the deps layer
├── docs/                         # README assets (architecture.jpg)
└── tests/                        # unit tests (moto + mocked Bedrock/Textract)
```

---

## 17. Build phases (each independently deployable)

0. **Foundation** — CDK skeleton + storage stack (S3 + lifecycle, DynamoDB, S3
   Tables, KMS).
1. **Open-table sink** — Firehose → S3 Tables (Iceberg) with the record schema.
2. **Core pipeline** — Preprocess + Textract basic OCR + spatial memory + Step
   Functions; clear path → Reconcile → Sink.
3. **Escalation tiers** — low-cost LLM (text) and Bedrock Data Automation
   (visual), both training spatial memory; structured Textract as opt-in.
4. **Reconcile + packs** — reconciliation + compliance; industry packs.
5. **Archival + retrieval** — configurable lifecycle + optional (opt-in) S3 Object Lambda retrieval.
6. **Observability + analytics** — Athena cost queries; CloudWatch dashboard +
   alarms; `cost_model.py`.

---

## 18. Prerequisites (user, in their AWS account)

- AWS account + region supporting **S3 Tables**, **Lambda durable functions**, and
  **Bedrock Data Automation** (with the required data-automation profile enabled).
- **Amazon Bedrock model access** for the chosen model (text + vision).
- Enable S3 Tables integration with AWS analytics services (per region).
- `cdk bootstrap` once per account/region; deploy via `cdk deploy` with the user's
  credentials.

---

## 19. References

- AWS GenAI IDP Accelerator — `aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws`
- Amazon S3 Tables (managed Iceberg) — docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables.html
- Amazon Data Firehose → Apache Iceberg / S3 Tables — docs.aws.amazon.com/firehose/latest/dev/apache-iceberg-destination.html
- Amazon Bedrock Data Automation — docs.aws.amazon.com/bedrock/latest/userguide/bda.html
- Amazon Textract — docs.aws.amazon.com/textract/latest/dg/what-is-textract.html
- AWS Lambda durable functions — docs.aws.amazon.com/lambda/latest/dg/durable-functions.html

*Prices are indicative list prices (us-east-1) gathered from public sources and
are subject to change; validate against the AWS Pricing Calculator for your
region and contract.*
