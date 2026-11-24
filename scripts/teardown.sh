#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Tear the sample down to a clean slate so it can be redeployed/retested.
#
# Destroys all CDK stacks (including the optional retrieval stack), then force-removes the RETAINED, fixed-name resources
# that would otherwise collide on the next deploy:
#   - the S3 Tables bucket  "<appName>-tables"  (+ its namespaces/tables)
#   - the Bedrock Data Automation project  "<appName>-visual"
#   - the BDA blueprint(s)  "<appName>-*-doc"
#
# Idempotent and safe to re-run: each step skips if the resource is absent.
#
# TIP: for a fully self-cleaning test cycle, deploy with
#   cdk deploy --all -c idp:destroyOnDelete=true
# so every resource (buckets, tables, keys, BDA project) uses a DESTROY removal
# policy and auto-empties. This script still runs cleanly either way.
#
# Usage:
#   ./scripts/teardown.sh                       # stacks + fixed-name retained resources
#   ./scripts/teardown.sh --purge               # also delete tagged retained buckets/tables
#   ./scripts/teardown.sh --remove-integration  # also remove the Region-level S3 Tables
#                                               # integration (s3tablescatalog + LF + role)
#   ./scripts/teardown.sh --purge --remove-integration   # complete wipe of everything created
#   APP_NAME=my-idp ./scripts/teardown.sh       # override the app-name prefix
#
# NOTE on --remove-integration: the S3 Tables integration is a Region-level
# singleton shared by every S3 Tables bucket/app in the account. Removing it is
# only safe if nothing else in this account/Region uses S3 Tables. Without this
# flag the integration is intentionally left in place (it is account/Region
# bootstrap, like `cdk bootstrap`).

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
APP_NAME="${APP_NAME:-cost-aware-idp}"
PURGE="false"
REMOVE_INTEGRATION="false"
for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE="true" ;;
    --remove-integration) REMOVE_INTEGRATION="true" ;;
    *) echo "Unknown flag: ${arg} (valid: --purge, --remove-integration)" >&2; exit 1 ;;
  esac
done

if [[ -z "${REGION}" ]]; then
  echo "ERROR: no region resolved. Set AWS_REGION or run 'aws configure set region <region>'." >&2
  exit 1
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
LF_ROLE_NAME="S3TablesRoleForLakeFormation"
BUCKET_WILDCARD_ARN="arn:aws:s3tables:${REGION}:${ACCOUNT}:bucket/*"
echo "=============================================================="
echo " Teardown   Account=${ACCOUNT}  Region=${REGION}  App=${APP_NAME}"
echo "            Purge=${PURGE}  RemoveIntegration=${REMOVE_INTEGRATION}"
echo "=============================================================="

# ---------------------------------------------------------------------------
# 1. Destroy all CDK stacks
# ---------------------------------------------------------------------------
echo ""
echo "[1/4] cdk destroy --all"
# -c idp:retrievalEnabled=true includes the optional retrieval stack so a
# deployment that opted in is removed too; CDK skips stacks never deployed.
cdk destroy --all --force -c idp:retrievalEnabled=true || echo "  cdk destroy reported a non-zero exit (stacks may already be gone); continuing."

# ---------------------------------------------------------------------------
# 2. Remove the retained S3 Tables bucket (fixed name) + its contents
# ---------------------------------------------------------------------------
echo ""
echo "[2/4] S3 Tables bucket: ${APP_NAME}-tables"
TB_ARN="$(aws s3tables list-table-buckets --region "${REGION}" \
  --query "tableBuckets[?name=='${APP_NAME}-tables'].arn" --output text 2>/dev/null || true)"
if [[ -n "${TB_ARN}" && "${TB_ARN}" != "None" ]]; then
  for NS in $(aws s3tables list-namespaces --region "${REGION}" --table-bucket-arn "${TB_ARN}" \
                --query "namespaces[].namespace" --output text 2>/dev/null || true); do
    for T in $(aws s3tables list-tables --region "${REGION}" --table-bucket-arn "${TB_ARN}" --namespace "${NS}" \
                 --query "tables[].name" --output text 2>/dev/null || true); do
      echo "  deleting table ${NS}.${T}"
      aws s3tables delete-table --region "${REGION}" --table-bucket-arn "${TB_ARN}" --namespace "${NS}" --name "${T}" || true
    done
    echo "  deleting namespace ${NS}"
    aws s3tables delete-namespace --region "${REGION}" --table-bucket-arn "${TB_ARN}" --namespace "${NS}" || true
  done
  echo "  deleting table bucket ${APP_NAME}-tables"
  aws s3tables delete-table-bucket --region "${REGION}" --table-bucket-arn "${TB_ARN}"
  # S3 Tables bucket deletion is asynchronous; the fixed bucket name stays in a
  # transitional state for a while and a redeploy that recreates it will fail
  # with "bucket is in a transitional state". Wait until it fully clears.
  echo "  waiting for '${APP_NAME}-tables' to finish deleting (async)..."
  for _ in $(seq 1 30); do
    if [[ -z "$(aws s3tables list-table-buckets --region "${REGION}" \
          --query "tableBuckets[?name=='${APP_NAME}-tables'].name" --output text)" ]]; then
      break
    fi
    sleep 20
  done
  echo "  table bucket removed. Note: AWS may take a few more minutes to release"
  echo "  the name internally; if the next deploy reports 'transitional state', wait and retry."
else
  echo "  not present — skipping."
fi

# ---------------------------------------------------------------------------
# 3. Remove the retained BDA project + blueprint(s) (fixed names)
# ---------------------------------------------------------------------------
echo ""
echo "[3/4] Bedrock Data Automation project + blueprints"
for PROJ_ARN in $(aws bedrock-data-automation list-data-automation-projects --region "${REGION}" \
    --query "projects[?projectName=='${APP_NAME}-visual'].projectArn" --output text 2>/dev/null || true); do
  echo "  deleting BDA project ${PROJ_ARN}"
  aws bedrock-data-automation delete-data-automation-project --region "${REGION}" --project-arn "${PROJ_ARN}" || true
done
for BP_ARN in $(aws bedrock-data-automation list-blueprints --region "${REGION}" \
    --query "blueprints[?starts_with(blueprintName, '${APP_NAME}-') && ends_with(blueprintName, '-doc')].blueprintArn" \
    --output text 2>/dev/null || true); do
  echo "  deleting BDA blueprint ${BP_ARN}"
  aws bedrock-data-automation delete-blueprint --region "${REGION}" --blueprint-arn "${BP_ARN}" || true
done
echo "  done (any 'not found' above is fine)."

# ---------------------------------------------------------------------------
# 4. Optional deep purge of other RETAINED, tagged resources
# ---------------------------------------------------------------------------
echo ""
echo "[4/4] Optional purge of tagged retained resources"
if [[ "${PURGE}" == "true" ]]; then
  echo "  --purge: removing S3 buckets and DynamoDB tables tagged project=${APP_NAME}"
  for ARN in $(aws resourcegroupstaggingapi get-resources --region "${REGION}" \
      --tag-filters "Key=project,Values=${APP_NAME}" --resource-type-filters s3 \
      --query "ResourceTagMappingList[].ResourceARN" --output text 2>/dev/null || true); do
    B="${ARN##*:}"
    echo "  emptying + deleting bucket ${B}"
    aws s3 rm "s3://${B}" --recursive >/dev/null 2>&1 || true
    aws s3api delete-bucket --bucket "${B}" --region "${REGION}" 2>/dev/null || true
  done
  for ARN in $(aws resourcegroupstaggingapi get-resources --region "${REGION}" \
      --tag-filters "Key=project,Values=${APP_NAME}" --resource-type-filters dynamodb:table \
      --query "ResourceTagMappingList[].ResourceARN" --output text 2>/dev/null || true); do
    T="${ARN##*/}"
    echo "  deleting DynamoDB table ${T}"
    aws dynamodb delete-table --table-name "${T}" --region "${REGION}" >/dev/null 2>&1 || true
  done
  echo "  Note: retained KMS keys are left with their pending-deletion window; remove them"
  echo "  from the KMS console if you want them gone sooner."
else
  echo "  skipped. Retained, auto-named resources (input/BDA-output/error/results"
  echo "  buckets, DynamoDB tables, KMS keys) do not block redeploy but do persist."
  echo "  Re-run with '--purge' to delete the tagged buckets/tables too."
fi

# ---------------------------------------------------------------------------
# 5. Optional: remove the Region-level S3 Tables integration (SHARED singleton)
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] S3 Tables analytics integration (Region-level, shared)"
if [[ "${REMOVE_INTEGRATION}" == "true" ]]; then
  echo "  WARNING: removing the s3tablescatalog + Lake Formation registration +"
  echo "  ${LF_ROLE_NAME} affects EVERY S3 Tables bucket/app in ${REGION}, not just"
  echo "  this sample. Only safe if nothing else in this account/Region uses S3 Tables."
  aws glue delete-catalog --catalog-id s3tablescatalog --region "${REGION}" 2>/dev/null \
    && echo "  deleted s3tablescatalog" || echo "  s3tablescatalog not present / not deleted."
  aws lakeformation deregister-resource --resource-arn "${BUCKET_WILDCARD_ARN}" --region "${REGION}" 2>/dev/null \
    && echo "  deregistered Lake Formation location" || echo "  location not registered / not deregistered."
  aws iam delete-role-policy --role-name "${LF_ROLE_NAME}" --policy-name S3TablesLakeFormationAccess 2>/dev/null || true
  aws iam delete-role --role-name "${LF_ROLE_NAME}" 2>/dev/null \
    && echo "  deleted IAM role ${LF_ROLE_NAME}" || echo "  IAM role not present / not deleted."
else
  echo "  kept (shared Region bootstrap). Re-run with '--remove-integration' to remove it."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
cat <<SUMMARY

==============================================================
 Teardown summary
--------------------------------------------------------------
 REMOVED:
   - All CDK stacks (cost-aware-idp-*)
   - S3 Tables bucket ${APP_NAME}-tables (+ namespaces/tables)
   - Bedrock Data Automation project + blueprint(s)
$( [[ "${PURGE}" == "true" ]] && echo "   - Tagged S3 buckets + DynamoDB tables (--purge)" || echo "   - (skipped tagged buckets/tables; pass --purge to remove them)" )
$( [[ "${REMOVE_INTEGRATION}" == "true" ]] && echo "   - s3tablescatalog + Lake Formation registration + LF role (--remove-integration)" || echo "   - (kept S3 Tables integration; pass --remove-integration to remove it)" )

 LEFT IN PLACE (shared account/Region bootstrap, not owned by this app):
   - CDK bootstrap stack (CDKToolkit)
$( [[ "${REMOVE_INTEGRATION}" == "true" ]] || echo "   - s3tablescatalog federated catalog + LF registration + LF role" )
   - KMS keys (retained with their pending-deletion window)

 For a complete wipe of everything this solution created:
   ./scripts/teardown.sh --purge --remove-integration
==============================================================
SUMMARY
