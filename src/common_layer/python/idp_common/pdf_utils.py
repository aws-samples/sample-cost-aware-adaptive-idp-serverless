# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pure-Python PDF helpers built on **pypdf** (BSD-3-Clause).

These utilities support the escalation path: when Textract basic OCR is not
enough and a document (or specific pages) must be handed to a cheap LLM for
structuring, we split the PDF per page and/or pull any embedded text layer.

pypdf is pure-Python (no native/Docker build) and permissively licensed, so it
fits the deps Lambda layer cleanly. It is imported lazily so this module and
its unit tests can run without pypdf installed.

Note: pypdf ``extract_text`` returns reading-order text without reliable
bounding boxes. Spatial coordinates for the memory store come from Textract OCR
or BDA visual grounding, not from this module.
"""

import io
from typing import List


def _load_reader(pdf_bytes: bytes):
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only in Lambda
        raise RuntimeError(
            "pypdf is required for PDF split/extract. "
            "Install it via the dependencies Lambda layer."
        ) from exc
    return PdfReader(io.BytesIO(pdf_bytes))


def page_count(pdf_bytes: bytes) -> int:
    """Return the number of pages in the PDF."""
    return len(_load_reader(pdf_bytes).pages)


def extract_page_text(pdf_bytes: bytes) -> List[str]:
    """Return the extracted text layer for each page (empty string if none).

    A page yielding an empty/whitespace-only string has no usable text layer
    (e.g. a scanned image) and is a candidate for OCR/BDA escalation.
    """
    reader = _load_reader(pdf_bytes)
    texts: List[str] = []
    for page in reader.pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:  # pragma: no cover - malformed page content
            texts.append("")
    return texts


def split_pages(pdf_bytes: bytes) -> List[bytes]:
    """Split a multi-page PDF into a list of single-page PDF byte blobs."""
    from pypdf import PdfWriter  # type: ignore

    reader = _load_reader(pdf_bytes)
    outputs: List[bytes] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buffer = io.BytesIO()
        writer.write(buffer)
        outputs.append(buffer.getvalue())
    return outputs


def extract_single_page(pdf_bytes: bytes, page_index: int) -> bytes:
    """Return a single-page PDF (0-based ``page_index``) as bytes."""
    from pypdf import PdfWriter  # type: ignore

    reader = _load_reader(pdf_bytes)
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
