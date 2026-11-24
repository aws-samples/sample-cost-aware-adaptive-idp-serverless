#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build the third-party dependencies Lambda layer
# (pypdf, PyYAML, aws-durable-execution-sdk-python).
#
# Installs the wheels for the Lambda runtime's platform into
# src/deps_layer/python so `cdk deploy` packages a working layer. Run this once
# before deploying (and whenever requirements change).
#
# Requires: pip (Python 3.14). All dependencies are pure-Python, so the
# platform flags below simply pin the runtime; no native build is required.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAYER_DIR="${SCRIPT_DIR}/../src/deps_layer"
TARGET="${LAYER_DIR}/python"
PY_VERSION="${PY_VERSION:-3.14}"

echo "Building deps layer into ${TARGET} for python ${PY_VERSION} (manylinux2014_x86_64)..."
rm -rf "${TARGET:?}"/*
mkdir -p "${TARGET}"

pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version "${PY_VERSION}" \
  --only-binary=:all: \
  --target "${TARGET}" \
  -r "${LAYER_DIR}/requirements.txt"

# Keep the placeholder so the directory is never empty in version control.
touch "${TARGET}/.gitkeep"
echo "Done. Layer contents:"
ls -1 "${TARGET}" | head -20
