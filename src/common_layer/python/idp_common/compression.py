# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Optional source-document compression for low-cost archival.

The compression algorithm used for a stored object is recorded in the
record's provenance (``compression_algo``) and in object metadata, so the
optional retrieval Object Lambda (idp:retrievalEnabled) can transparently
decompress on GET.

``gzip`` uses the standard library (zero dependencies). ``zstd`` is supported
when the ``zstandard`` package is available (it is not required).
"""

import gzip
from typing import Dict, Optional

ALGO_NONE = "none"
ALGO_GZIP = "gzip"
ALGO_ZSTD = "zstd"

# Object metadata header (S3 lowercases ``x-amz-meta-*`` keys).
METADATA_KEY = "compression-algo"

_EXT_ALGO = {".gz": ALGO_GZIP, ".zst": ALGO_ZSTD, ".zstd": ALGO_ZSTD}


def compress(data: bytes, algo: str) -> bytes:
    if algo in (None, ALGO_NONE):
        return data
    if algo == ALGO_GZIP:
        return gzip.compress(data)
    if algo == ALGO_ZSTD:
        import zstandard  # type: ignore

        return zstandard.ZstdCompressor().compress(data)
    raise ValueError(f"Unsupported compression algo: {algo}")


def decompress(data: bytes, algo: str) -> bytes:
    if algo in (None, ALGO_NONE):
        return data
    if algo == ALGO_GZIP:
        return gzip.decompress(data)
    if algo == ALGO_ZSTD:
        import zstandard  # type: ignore

        return zstandard.ZstdDecompressor().decompress(data)
    raise ValueError(f"Unsupported compression algo: {algo}")


def detect_algo(key: str, metadata: Optional[Dict[str, str]] = None) -> str:
    """Determine the compression algo from object metadata, else key extension."""
    if metadata:
        # Metadata keys may arrive with or without the x-amz-meta- prefix.
        for candidate in (METADATA_KEY, f"x-amz-meta-{METADATA_KEY}"):
            if candidate in metadata and metadata[candidate]:
                return metadata[candidate]
    lower = key.lower()
    for ext, algo in _EXT_ALGO.items():
        if lower.endswith(ext):
            return algo
    return ALGO_NONE
