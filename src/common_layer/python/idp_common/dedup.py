# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Content-hash deduplication / cache (Tier 0, free).

Identical re-uploads should never be reprocessed. We key on the SHA-256 of the
raw bytes and remember which template/doc a hash resolved to, with a TTL so the
cache self-expires.

Only *completed* documents count as duplicates. The lifecycle of an item is:

1. ``claim`` (preprocess): a conditional put writes ``status=PROCESSING`` with a
   lease (``lease_expires_at``). A second upload of the same bytes while the
   lease is live sees ``in_progress`` and is skipped.
2. ``complete`` (sink): after the record is delivered, ``status=DONE`` and the
   30-day TTL start. From then on the hash is a duplicate.
3. ``release`` (state-machine failure path): the PROCESSING claim is deleted so
   the same file can be re-uploaded and processed.

If neither ``complete`` nor ``release`` runs (for example the execution hits
its timeout), the lease lapses and the next upload takes the claim over.
Items written before this lifecycle existed have no ``status`` and are treated
as ``DONE``.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Callable, Optional

STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"


def content_hash(data: bytes) -> str:
    """Stable SHA-256 hex digest of the document bytes."""
    return hashlib.sha256(data).hexdigest()


@dataclass
class DedupResult:
    """Outcome of a dedup lookup or claim."""

    content_hash: str
    is_duplicate: bool
    template_id: Optional[str] = None
    first_doc_id: Optional[str] = None
    # Another execution holds a live PROCESSING claim on this hash.
    in_progress: bool = False
    # This call created (or took over) the PROCESSING claim.
    acquired: bool = False


class DedupIndex:
    """DynamoDB-backed content-hash index with TTL and an in-progress lease.

    Item shape: ``{content_hash, doc_id, source_uri, status, lease_expires_at,
    template_id, expires_at}``.
    """

    def __init__(
        self,
        table_name: str,
        client=None,
        ttl_days: int = 30,
        clock: Callable[[], float] = time.time,
    ):
        import boto3  # type: ignore  # lazy import keeps module test-friendly

        self._table_name = table_name
        self._ttl_seconds = ttl_days * 24 * 3600
        self._client = client or boto3.client("dynamodb")
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock())

    def check(self, digest: str) -> DedupResult:
        resp = self._client.get_item(
            TableName=self._table_name,
            Key={"content_hash": {"S": digest}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item:
            return DedupResult(content_hash=digest, is_duplicate=False)
        doc_id = item.get("doc_id", {}).get("S")
        status = item.get("status", {}).get("S")
        if status == STATUS_PROCESSING:
            lease = int(item.get("lease_expires_at", {}).get("N", "0"))
            return DedupResult(
                content_hash=digest,
                is_duplicate=False,
                first_doc_id=doc_id,
                in_progress=lease >= self._now(),
            )
        # DONE, or a legacy item with no status.
        return DedupResult(
            content_hash=digest,
            is_duplicate=True,
            template_id=item.get("template_id", {}).get("S"),
            first_doc_id=doc_id,
        )

    def claim(
        self,
        digest: str,
        doc_id: str,
        source_uri: str,
        lease_seconds: int,
    ) -> DedupResult:
        """Take the PROCESSING claim for ``digest`` unless it is held or done.

        Succeeds when no item exists or the existing claim's lease has lapsed.
        Otherwise returns the current state (duplicate or in progress).
        """
        now = self._now()
        lease_expires_at = now + lease_seconds
        item = {
            "content_hash": {"S": digest},
            "doc_id": {"S": doc_id},
            "source_uri": {"S": source_uri},
            "status": {"S": STATUS_PROCESSING},
            "lease_expires_at": {"N": str(lease_expires_at)},
            # TTL also removes a claim nobody completed or released.
            "expires_at": {"N": str(lease_expires_at + self._ttl_seconds)},
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(content_hash) OR "
                    "(#status = :processing AND lease_expires_at < :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": {"S": STATUS_PROCESSING},
                    ":now": {"N": str(now)},
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return self.check(digest)
        return DedupResult(
            content_hash=digest,
            is_duplicate=False,
            first_doc_id=doc_id,
            acquired=True,
        )

    def complete(
        self, digest: str, doc_id: str, template_id: Optional[str] = None
    ) -> bool:
        """Mark ``doc_id``'s claim DONE. Returns False if another doc holds it."""
        update = "SET #status = :done, expires_at = :exp"
        values = {
            ":done": {"S": STATUS_DONE},
            ":exp": {"N": str(self._now() + self._ttl_seconds)},
            ":doc": {"S": doc_id},
        }
        if template_id:
            update += ", template_id = :tid"
            values[":tid"] = {"S": template_id}
        update += " REMOVE lease_expires_at"
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"content_hash": {"S": digest}},
                UpdateExpression=update,
                ConditionExpression="doc_id = :doc",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def release(self, digest: str, doc_id: str) -> bool:
        """Delete ``doc_id``'s PROCESSING claim so the file can be re-uploaded.

        A no-op (returns False) if the claim was taken over or already DONE.
        """
        try:
            self._client.delete_item(
                TableName=self._table_name,
                Key={"content_hash": {"S": digest}},
                ConditionExpression="doc_id = :doc AND #status = :processing",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":doc": {"S": doc_id},
                    ":processing": {"S": STATUS_PROCESSING},
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return False
        return True
