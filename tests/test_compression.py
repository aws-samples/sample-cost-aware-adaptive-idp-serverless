# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for optional compression and algo detection."""

import pytest

from idp_common import compression


@pytest.mark.parametrize("algo", [compression.ALGO_NONE, compression.ALGO_GZIP])
def test_compress_decompress_roundtrip(algo):
    data = b"the quick brown fox " * 50
    packed = compression.compress(data, algo)
    assert compression.decompress(packed, algo) == data


def test_gzip_actually_shrinks_repetitive_data():
    data = b"A" * 10_000
    assert len(compression.compress(data, compression.ALGO_GZIP)) < len(data)


def test_none_is_passthrough():
    data = b"abc"
    assert compression.compress(data, compression.ALGO_NONE) is data
    assert compression.decompress(data, compression.ALGO_NONE) is data


def test_unsupported_algo_raises():
    with pytest.raises(ValueError):
        compression.compress(b"x", "lz4")


def test_detect_algo_from_metadata():
    md = {"x-amz-meta-compression-algo": "gzip"}
    assert compression.detect_algo("doc.pdf", md) == "gzip"
    md2 = {"compression-algo": "zstd"}
    assert compression.detect_algo("doc.pdf", md2) == "zstd"


def test_detect_algo_from_extension():
    assert compression.detect_algo("scan.pdf.gz") == compression.ALGO_GZIP
    assert compression.detect_algo("scan.pdf.zst") == compression.ALGO_ZSTD
    assert compression.detect_algo("scan.pdf") == compression.ALGO_NONE
