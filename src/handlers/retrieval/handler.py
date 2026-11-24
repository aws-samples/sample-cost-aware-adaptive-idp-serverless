# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Retrieval Lambda — S3 Object Lambda transparent decompression on GET.

Deployed only when the optional retrieval stack is enabled
(``-c idp:retrievalEnabled=true``).

Fronts the source bucket via an S3 Object Lambda Access Point. When a GET
arrives, S3 hands us a presigned URL to the stored (possibly compressed)
object plus a response route/token. We fetch the bytes, decompress them based
on the recorded algorithm (object metadata or key extension), and stream the
original document back to the caller via ``WriteGetObjectResponse``.

Auditors therefore always read the original document, regardless of how it was
archived. ``source_uri`` + ``compression_algo`` in the provenance record drive
which object is retrieved.
"""

import http.client
import logging
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

import boto3

from idp_common import compression

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3")


def _fetch(url: str) -> bytes:
    """Fetch the presigned S3 URL S3 Object Lambda provides for the object.

    Only HTTPS URLs on an AWS (S3) host are accepted, so the handler can never
    be pointed at a local file or an arbitrary host.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn")
    ):
        raise ValueError("inputS3Url must be an https URL on an amazonaws.com host")
    conn = http.client.HTTPSConnection(host, parsed.port or 443, timeout=30)
    try:
        conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"GET input object failed: HTTP {resp.status}")
        return body
    finally:
        conn.close()


def _object_key_from_url(url: str) -> str:
    return unquote(urlparse(url).path.lstrip("/"))


def _algo_from_headers(headers: Optional[Dict[str, str]], key: str) -> str:
    metadata = {}
    for name, value in (headers or {}).items():
        metadata[name.lower()] = value
    return compression.detect_algo(key, metadata)


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    context = event["getObjectContext"]
    input_url = context["inputS3Url"]
    route = context["outputRoute"]
    token = context["outputToken"]

    user_request = event.get("userRequest", {})
    key = _object_key_from_url(user_request.get("url", input_url))

    raw = _fetch(input_url)
    algo = _algo_from_headers(user_request.get("headers"), key)
    body = compression.decompress(raw, algo)
    logger.info("Retrieval: key=%s algo=%s in=%dB out=%dB", key, algo, len(raw), len(body))

    _s3.write_get_object_response(RequestRoute=route, RequestToken=token, Body=body)
    return {"statusCode": 200}
