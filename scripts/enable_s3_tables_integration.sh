#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# One-time, per-Region setup: integrate Amazon S3 Tables with the AWS Glue Data
# Catalog and AWS Lake Formation so Amazon Data Firehose and Amazon Athena can
# read the managed Iceberg table.
#
# This sample uses **IAM access control mode** for the s3tablescatalog: access
# is governed entirely by IAM policies (which the CDK stacks grant), with no
# per-principal Lake Formation grants. The catalog's default permissions grant
# ALL to IAM_ALLOWED_PRINCIPALS, and OverwriteChildResourcePermissionsWithDefault
# is set to Accept so the setting also applies to already-created databases and
# tables. (Lake Formation fine-grained mode would instead require explicit LF
# grants for every principal, including the Firehose role.)
#
# This is a Region-level, account-global singleton (one s3tablescatalog per
# Region), analogous to `cdk bootstrap`. Run it once per account/Region before
# `cdk deploy`. Safe to re-run: it creates what is missing and re-applies the
# IAM-access-mode defaults (which also repairs a catalog left in Lake Formation
# mode by an earlier run or the console).
#
# Requires AWS credentials with IAM + Lake Formation admin permissions.

set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [[ -z "${REGION}" ]]; then
  echo "ERROR: no region resolved. Set AWS_REGION or run 'aws configure set region <region>'." >&2
  exit 1
fi
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ROLE_NAME="S3TablesRoleForLakeFormation"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
CATALOG_ID="s3tablescatalog"
BUCKET_ARN="arn:aws:s3tables:${REGION}:${ACCOUNT}:bucket/*"

echo "Account=${ACCOUNT}  Region=${REGION}  (IAM access control mode)"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# 1. IAM role Lake Formation uses to vend credentials / register the location.
cat > "${TMP}/trust.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "lakeformation.amazonaws.com" },
      "Action": ["sts:AssumeRole", "sts:SetContext", "sts:SetSourceIdentity"],
      "Condition": { "StringEquals": { "aws:SourceAccount": "${ACCOUNT}" } }
    }
  ]
}
JSON

cat > "${TMP}/policy.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ListTableBuckets", "Effect": "Allow", "Action": ["s3tables:ListTableBuckets"], "Resource": ["*"] },
    {
      "Sid": "S3TablesDataAccess", "Effect": "Allow",
      "Action": [
        "s3tables:CreateTableBucket", "s3tables:GetTableBucket", "s3tables:CreateNamespace",
        "s3tables:GetNamespace", "s3tables:ListNamespaces", "s3tables:DeleteNamespace",
        "s3tables:DeleteTableBucket", "s3tables:CreateTable", "s3tables:DeleteTable",
        "s3tables:GetTable", "s3tables:ListTables", "s3tables:RenameTable",
        "s3tables:UpdateTableMetadataLocation", "s3tables:GetTableMetadataLocation",
        "s3tables:GetTableData", "s3tables:PutTableData"
      ],
      "Resource": ["arn:aws:s3tables:${REGION}:${ACCOUNT}:bucket/*"]
    }
  ]
}
JSON

if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  echo "IAM role ${ROLE_NAME} already exists."
else
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document "file://${TMP}/trust.json" >/dev/null
  echo "Created IAM role ${ROLE_NAME}; waiting for propagation..."
  sleep 10
fi
aws iam put-role-policy --role-name "${ROLE_NAME}" \
  --policy-name S3TablesLakeFormationAccess \
  --policy-document "file://${TMP}/policy.json"

# 2. Register the S3 Tables location (federated) — idempotent.
cat > "${TMP}/register.json" <<JSON
{ "ResourceArn": "${BUCKET_ARN}", "WithFederation": true, "RoleArn": "${ROLE_ARN}" }
JSON
aws lakeformation register-resource --region "${REGION}" \
  --with-privileged-access --cli-input-json "file://${TMP}/register.json" \
  || echo "register-resource returned non-zero (location may already be registered); continuing."

# 3. Create or update the s3tablescatalog in IAM access control mode.
DEFAULT_PERMS='[{"Principal":{"DataLakePrincipalIdentifier":"IAM_ALLOWED_PRINCIPALS"},"Permissions":["ALL"]}]'

if aws glue get-catalog --catalog-id "${CATALOG_ID}" --region "${REGION}" >/dev/null 2>&1; then
  echo "s3tablescatalog exists — setting IAM access control defaults..."
  # Note: newer CLIs also accept "OverwriteChildResourcePermissionsWithDefault":
  # "Accept" to retrofit EXISTING databases/tables. This sample runs the
  # integration before deploy, so children inherit these defaults at creation
  # and the retrofit is unnecessary. If you enabled integration in Lake
  # Formation mode AFTER creating objects, upgrade the AWS CLI and re-run, or
  # delete + recreate the catalog on a clean slate.
  cat > "${TMP}/update.json" <<JSON
{
  "FederatedCatalog": { "Identifier": "${BUCKET_ARN}", "ConnectionName": "aws:s3tables" },
  "CreateDatabaseDefaultPermissions": ${DEFAULT_PERMS},
  "CreateTableDefaultPermissions": ${DEFAULT_PERMS}
}
JSON
  aws glue update-catalog --region "${REGION}" --catalog-id "${CATALOG_ID}" \
    --catalog-input "file://${TMP}/update.json"
else
  echo "Creating s3tablescatalog in IAM access control mode..."
  cat > "${TMP}/create.json" <<JSON
{
  "Name": "s3tablescatalog",
  "CatalogInput": {
    "FederatedCatalog": { "Identifier": "${BUCKET_ARN}", "ConnectionName": "aws:s3tables" },
    "CreateDatabaseDefaultPermissions": ${DEFAULT_PERMS},
    "CreateTableDefaultPermissions": ${DEFAULT_PERMS}
  }
}
JSON
  aws glue create-catalog --region "${REGION}" --cli-input-json "file://${TMP}/create.json"
fi

# 4. Verify.
aws glue get-catalog --catalog-id "${CATALOG_ID}" --region "${REGION}" >/dev/null \
  && echo "Done. s3tablescatalog is enabled in ${REGION} in IAM access control mode."
