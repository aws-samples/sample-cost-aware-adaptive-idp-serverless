#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Cost calculator: $/1,000 docs per tier, blended, storage, and naive baseline.

Makes the "AI only when needed" savings concrete and editable. Prices are
indicative AWS list prices (us-east-1) gathered from public sources and are
subject to change — validate against the AWS Pricing Calculator for your
region and contract. Tune ``PRICES`` and the token estimates as needed.

The cascade this models:

* ``low_cost_ocr``  — Textract ``DetectDocumentText`` (basic OCR) + free spatial
  memory. The default, and the target tier for most documents.
* ``llm``        — OCR unclear (text): escalate to a cheap LLM (Nova). OCR has
  already run, so cost = OCR + LLM tokens.
* ``bda``        — OCR unclear (image/chart/hard): escalate to Bedrock Data
  Automation. Cost = OCR + BDA per-page.
* ``structured`` — opt-in Textract FORMS/TABLES (``AnalyzeDocument``), the most
  expensive OCR mode; off by default.
* ``full_cycle`` — worst case: OCR + structured + LLM (the naive baseline runs
  something like this — or structured — on *every* document).

Usage:
    python tools/cost_model.py \
        --low-cost-ocr 0.70 --llm 0.20 --bda 0.08 --structured 0.02 \
        --pages 1 --model nova-lite --docs 1000
"""

import argparse
from dataclasses import dataclass
from typing import Dict

# --- Indicative list prices (us-east-1). Clearly labeled; edit freely. -------
PRICES = {
    "textract_detect_per_page": 1.50 / 1000,  # DetectDocumentText ($1.50/1k pg)
    "textract_analyze_per_page": 65.0 / 1000,  # AnalyzeDocument Forms+Tables
    "bda_per_page": 40.0 / 1000,  # Bedrock Data Automation custom output (~$0.04/pg)
    "sfn_per_transition": 0.025 / 1000,  # Standard workflow
    "lambda_per_request": 0.20 / 1_000_000,
    "lambda_gb_second": 0.0000166667,
    "firehose_per_gb": 0.029,  # per-GB ingested (5 KB increments)
    "athena_per_tb": 5.0,
    "s3_glacier_ir_gb_month": 0.004,  # archived source after tiering
}

# Bedrock per-1M-token prices: (input, output).
BEDROCK_PRICES = {
    "nova-lite": (0.06, 0.24),
    "claude-haiku": (0.25, 1.25),
    "claude-sonnet": (3.0, 15.0),
}

# Rough per-document compute assumptions.
SFN_TRANSITIONS_PER_DOC = 6
LAMBDA_INVOCATIONS_PER_DOC = 4
LAMBDA_AVG_GB = 0.5
LAMBDA_AVG_SECONDS = 2.0
# LLM token estimates per doc (vision adds image tokens).
LLM_INPUT_TOKENS = 3000
LLM_OUTPUT_TOKENS = 600
# Stored record + source sizing.
AVG_SOURCE_MB = 0.5


@dataclass
class CostInputs:
    """Document mix (fractions that must sum to 1.0) + scenario knobs."""

    low_cost_ocr: float = 0.70
    llm: float = 0.20
    bda: float = 0.08
    structured: float = 0.02
    pages: float = 1.0
    model: str = "nova-lite"
    docs: int = 1000
    retain_source: bool = True
    annotations: bool = False

    def mix(self) -> Dict[str, float]:
        return {
            "low_cost_ocr": self.low_cost_ocr,
            "llm": self.llm,
            "bda": self.bda,
            "structured": self.structured,
        }


def _fixed_overhead() -> float:
    """Per-doc Lambda + Step Functions + Firehose overhead (tier-independent)."""
    lambda_cost = (
        LAMBDA_INVOCATIONS_PER_DOC * PRICES["lambda_per_request"]
        + LAMBDA_AVG_GB * LAMBDA_AVG_SECONDS * PRICES["lambda_gb_second"]
    )
    sfn_cost = SFN_TRANSITIONS_PER_DOC * PRICES["sfn_per_transition"]
    firehose_cost = (AVG_SOURCE_MB / 1024.0) * 0.01 * PRICES["firehose_per_gb"]
    return lambda_cost + sfn_cost + firehose_cost


def _bedrock_cost(model: str) -> float:
    in_price, out_price = BEDROCK_PRICES.get(model, BEDROCK_PRICES["nova-lite"])
    return (
        LLM_INPUT_TOKENS / 1_000_000 * in_price
        + LLM_OUTPUT_TOKENS / 1_000_000 * out_price
    )


def tier_cost_per_doc(tier: str, pages: float, model: str) -> float:
    """All-in cost for one document resolved at ``tier``.

    Every non-duplicate document runs basic OCR first; escalation tiers add
    their service cost on top of that OCR pass.
    """
    overhead = _fixed_overhead()
    ocr = PRICES["textract_detect_per_page"] * pages
    if tier == "dedup":
        return overhead  # duplicate: skipped before OCR
    if tier == "low_cost_ocr":
        return overhead + ocr
    if tier == "llm":
        return overhead + ocr + _bedrock_cost(model)
    if tier == "bda":
        return overhead + ocr + PRICES["bda_per_page"] * pages
    if tier == "structured":
        return overhead + ocr + PRICES["textract_analyze_per_page"] * pages
    if tier == "full_cycle":
        return (
            overhead
            + ocr
            + PRICES["textract_analyze_per_page"] * pages
            + _bedrock_cost(model)
        )
    raise ValueError(f"Unknown tier: {tier}")


def storage_cost_per_doc(retain_source: bool) -> float:
    if not retain_source:
        return 0.0
    return AVG_SOURCE_MB / 1024.0 * PRICES["s3_glacier_ir_gb_month"]


def compute(inputs: CostInputs) -> Dict[str, object]:
    mix = inputs.mix()
    total = sum(mix.values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"Document mix must sum to 1.0, got {total:.3f}")

    per_tier = {
        tier: tier_cost_per_doc(tier, inputs.pages, inputs.model) for tier in mix
    }
    blended_per_doc = sum(mix[t] * per_tier[t] for t in mix)
    storage = storage_cost_per_doc(inputs.retain_source)
    # Naive baseline = not cost-aware: run structured extraction on every doc.
    naive_per_doc = tier_cost_per_doc("structured", inputs.pages, inputs.model)

    scale = inputs.docs
    return {
        "model": inputs.model,
        "pages_per_doc": inputs.pages,
        "mix": mix,
        "per_tier_per_1000": {t: round(per_tier[t] * 1000, 4) for t in per_tier},
        "blended_per_1000": round(blended_per_doc * 1000, 4),
        "storage_per_1000_month": round(storage * 1000, 4),
        "naive_baseline_per_1000": round(naive_per_doc * 1000, 4),
        "savings_vs_naive_pct": round(
            (1 - blended_per_doc / naive_per_doc) * 100, 1
        )
        if naive_per_doc
        else 0.0,
        "blended_total": round(blended_per_doc * scale, 4),
        "docs": scale,
    }


def format_report(report: Dict[str, object]) -> str:
    lines = [
        "Cost-aware adaptive IDP — cost model",
        "=" * 44,
        f"Model: {report['model']}   Pages/doc: {report['pages_per_doc']}",
        "",
        "Per-tier cost ($ / 1,000 docs):",
    ]
    for tier, cost in report["per_tier_per_1000"].items():
        share = report["mix"][tier]
        lines.append(f"  {tier:<14} ${cost:>8.2f}   (mix {share*100:.0f}%)")
    lines += [
        "",
        f"Blended:        ${report['blended_per_1000']:>8.2f} / 1,000 docs",
        f"Storage:        ${report['storage_per_1000_month']:>8.2f} / 1,000 docs-month",
        f"Naive baseline: ${report['naive_baseline_per_1000']:>8.2f} / 1,000 docs",
        f"Savings vs naive: {report['savings_vs_naive_pct']}%",
        "",
        "Prices are indicative list prices (us-east-1); validate before quoting.",
    ]
    return "\n".join(lines)


def _parse_args(argv=None) -> CostInputs:
    p = argparse.ArgumentParser(description="Cost model for the adaptive IDP sample.")
    p.add_argument("--low-cost-ocr", type=float, default=0.70)
    p.add_argument("--llm", type=float, default=0.20)
    p.add_argument("--bda", type=float, default=0.08)
    p.add_argument("--structured", type=float, default=0.02)
    p.add_argument("--pages", type=float, default=1.0)
    p.add_argument("--model", choices=sorted(BEDROCK_PRICES), default="nova-lite")
    p.add_argument("--docs", type=int, default=1000)
    p.add_argument("--no-retain-source", action="store_true")
    p.add_argument("--annotations", action="store_true")
    args = p.parse_args(argv)
    return CostInputs(
        low_cost_ocr=args.low_cost_ocr,
        llm=args.llm,
        bda=args.bda,
        structured=args.structured,
        pages=args.pages,
        model=args.model,
        docs=args.docs,
        retain_source=not args.no_retain_source,
        annotations=args.annotations,
    )


def main(argv=None) -> int:
    inputs = _parse_args(argv)
    print(format_report(compute(inputs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
