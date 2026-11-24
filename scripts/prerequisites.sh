#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One-command prerequisite setup for the cost-aware adaptive IDP sample.
#
# Runs the scriptable, one-time account/Region bootstrap in order, and is safe
# to re-run: each step checks whether it is already done and skips if so.
#
#   1. CDK bootstrap                (skipped if the CDKToolkit stack exists)
#   2. S3 Tables analytics integration (skipped if s3tablescatalog exists)
#   3. Build the dependencies Lambda layer
#
# Two items cannot be scripted and are printed as reminders at the end:
#   - Amazon Bedrock model access (console/API opt-in; no CloudFormation resource)
#   - Bedrock Data Automation "us.data-automation-v1" profile enablement
#
# Prerequisites for running this script:
#   - AWS credentials configured (via `aws configure`, SSO, or environment
#     variables) and a region set (AWS_REGION / AWS_DEFAULT_REGION).
#   - Python venv active and `pip install -r requirements.txt` done (for CDK + build).
#   - Lake Formation admin permissions (for the S3 Tables integration).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"

if [[ -z "${REGION}" ]]; then
  echo "ERROR: no region resolved. Set AWS_REGION or run 'aws configure set region <region>'." >&2
  exit 1
fi

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
echo "=============================================================="
echo " Prerequisite setup   Account=${ACCOUNT}  Region=${REGION}"
echo "=============================================================="

# ---------------------------------------------------------------------------
# 1. CDK bootstrap (idempotent: skip if the CDKToolkit stack already exists)
# ---------------------------------------------------------------------------
echo ""
echo "[1/3] CDK bootstrap"
if aws cloudformation describe-stacks --stack-name CDKToolkit --region "${REGION}" >/dev/null 2>&1; then
  echo "  CDKToolkit already present in ${REGION} — skipping bootstrap."
else
  echo "  Bootstrapping aws://${ACCOUNT}/${REGION} ..."
  cdk bootstrap "aws://${ACCOUNT}/${REGION}"
fi

# ---------------------------------------------------------------------------
# 2. S3 Tables analytics integration (script is itself idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "[2/3] S3 Tables analytics integration"
"${SCRIPT_DIR}/enable_s3_tables_integration.sh"

# ---------------------------------------------------------------------------
# 3. Dependencies Lambda layer
# ---------------------------------------------------------------------------
echo ""
echo "[3/3] Build dependencies layer"
if [[ -d "${SCRIPT_DIR}/../src/deps_layer/python/pypdf" ]]; then
  echo "  Layer already populated (pypdf present). Rebuilding to stay current ..."
fi
"${SCRIPT_DIR}/build_deps_layer.sh"

# ---------------------------------------------------------------------------
# Bedrock Data Automation availability check
# ---------------------------------------------------------------------------
echo ""
echo "[check] Bedrock Data Automation availability in ${REGION}"
if aws bedrock-data-automation list-data-automation-projects --region "${REGION}" >/dev/null 2>&1; then
  echo "  OK — Bedrock Data Automation is reachable in ${REGION}."
else
  echo "  WARNING — could not reach Bedrock Data Automation in ${REGION}."
  echo "  It may not be available in this region, or your credentials lack access."
  echo "  Use a supported region (e.g. us-east-1, us-west-2), or the visual tier will fail."
fi

# ---------------------------------------------------------------------------
# Model access note
# ---------------------------------------------------------------------------
cat <<'NOTE'

==============================================================
 Prerequisites complete.

 Model access: Amazon Bedrock foundation models (including the
 default Amazon Nova) are enabled by default in commercial
 regions — no manual "model access" step is needed. Only if you
 set idp:bedrockModelId to a THIRD-PARTY model (e.g. Anthropic
 Claude) do you need aws-marketplace:Subscribe/ViewSubscriptions
 permissions and the one-time Anthropic use-case form on first use.

 Then deploy:  cdk deploy --all --require-approval never
==============================================================
NOTE
