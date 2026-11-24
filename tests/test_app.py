# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""App-level synth test: the retrieval stack is opt-in via idp:retrievalEnabled."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_STACKS = {"t-idp-storage", "t-idp-sink", "t-idp-pipeline", "t-idp-observability"}


@pytest.mark.parametrize(
    "extra_context, expected",
    [
        ({}, BASE_STACKS),
        ({"idp:retrievalEnabled": "false"}, BASE_STACKS),
        ({"idp:retrievalEnabled": True}, BASE_STACKS | {"t-idp-retrieval"}),
    ],
)
def test_app_synthesizes_retrieval_stack_only_when_enabled(tmp_path, extra_context, expected):
    ctx = {"idp:appName": "t-idp", **extra_context}
    subprocess.run(
        [sys.executable, "app.py"],
        cwd=REPO_ROOT,
        env={**os.environ, "CDK_OUTDIR": str(tmp_path), "CDK_CONTEXT_JSON": json.dumps(ctx)},
        check=True,
        capture_output=True,
        timeout=300,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    stacks = {
        name
        for name, artifact in manifest["artifacts"].items()
        if artifact.get("type") == "aws:cloudformation:stack"
    }
    assert stacks == expected
