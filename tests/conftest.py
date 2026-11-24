# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared test fixtures and import-path wiring.

Puts the Lambda layer's ``python/`` directory and each handler directory on
``sys.path`` so tests can import ``idp_common`` and the handlers exactly as
they resolve at runtime.
"""

import os
import sys
from pathlib import Path
from typing import List

import pytest

# Pin a region + dummy credentials so moto-backed clients (including those the
# handlers create internally without an explicit region) resolve consistently.
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

_ROOT = Path(__file__).resolve().parents[1]
_LAYER = _ROOT / "src" / "common_layer" / "python"
_HANDLERS = _ROOT / "src" / "handlers"

for path in (_LAYER, _HANDLERS / "preprocess", _HANDLERS / "spatial", _HANDLERS / "sink"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


from idp_common.models import BBox, Document, KeyValue, Page, Word  # noqa: E402


def make_document(fields: List[tuple], page: int = 1) -> Document:
    """Build a single-page Document from ``(name, x, y, w, h[, value])`` tuples."""
    kvs = []
    for spec in fields:
        name, x, y, w, h = spec[:5]
        value = spec[5] if len(spec) > 5 else ""
        kvs.append(
            KeyValue(
                key=[Word(tok) for tok in name.split()],
                bbox=BBox(x, y, w, h),
                page=page,
                value=value,
            )
        )
    return Document(pages=[Page(key_values=kvs, has_text_layer=True)])


@pytest.fixture
def employment_form():
    return make_document(
        [
            ("Employee Name", 0.05, 0.08, 0.35, 0.03, "Jane Doe"),
            ("Date of Birth", 0.05, 0.14, 0.20, 0.03, "1990-01-01"),
            ("SSN", 0.05, 0.20, 0.15, 0.03, "xxx-xx-1234"),
            ("Address", 0.05, 0.26, 0.45, 0.03, "1 Main St"),
            ("Phone Number", 0.05, 0.38, 0.20, 0.03, "555-0100"),
            ("Email", 0.05, 0.44, 0.30, 0.03, "jane@example.com"),
            ("Start Date", 0.05, 0.56, 0.15, 0.03, "2026-01-01"),
        ]
    )


@pytest.fixture
def invoice():
    return make_document(
        [
            ("Invoice Number", 0.60, 0.05, 0.20, 0.03, "INV-001"),
            ("Invoice Date", 0.60, 0.10, 0.15, 0.03, "2026-01-01"),
            ("Due Date", 0.60, 0.15, 0.15, 0.03, "2026-02-01"),
            ("Bill To", 0.05, 0.20, 0.30, 0.03, "ACME"),
            ("Total", 0.60, 0.80, 0.15, 0.03, "100.00"),
        ]
    )
