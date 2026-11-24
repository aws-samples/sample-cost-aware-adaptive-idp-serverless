# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the completion-aware content-hash dedup index (threat T4)."""

import boto3
import pytest

moto = pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from idp_common.dedup import DedupIndex  # noqa: E402

REGION = "us-east-1"
LEASE = 2100


class _Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def index():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="dedup",
            AttributeDefinitions=[{"AttributeName": "content_hash", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "content_hash", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        clock = _Clock()
        yield DedupIndex("dedup", client=ddb, clock=clock), ddb, clock


def test_claim_on_empty_table_is_acquired(index):
    dedup, _, _ = index
    res = dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    assert res.acquired is True
    assert res.is_duplicate is False
    assert res.in_progress is False
    assert res.first_doc_id == "doc-a"


def test_second_claim_while_live_is_in_progress(index):
    dedup, _, _ = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    res = dedup.claim("h1", "doc-b", "s3://b/b", LEASE)
    assert res.acquired is False
    assert res.in_progress is True
    assert res.is_duplicate is False
    assert res.first_doc_id == "doc-a"


def test_complete_makes_hash_a_duplicate(index):
    dedup, _, _ = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    assert dedup.check("h1").is_duplicate is False
    assert dedup.complete("h1", "doc-a", template_id="tpl-1") is True
    res = dedup.check("h1")
    assert res.is_duplicate is True
    assert res.in_progress is False
    assert res.first_doc_id == "doc-a"
    assert res.template_id == "tpl-1"
    # A completed hash cannot be claimed again.
    assert dedup.claim("h1", "doc-b", "s3://b/b", LEASE).is_duplicate is True


def test_release_allows_reclaim(index):
    dedup, _, _ = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    assert dedup.release("h1", "doc-a") is True
    assert dedup.check("h1").is_duplicate is False
    res = dedup.claim("h1", "doc-b", "s3://b/b", LEASE)
    assert res.acquired is True
    assert res.first_doc_id == "doc-b"


def test_expired_lease_is_taken_over(index):
    dedup, _, clock = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    clock.now += LEASE + 1
    stale = dedup.check("h1")
    assert stale.in_progress is False
    assert stale.is_duplicate is False
    res = dedup.claim("h1", "doc-b", "s3://b/b", LEASE)
    assert res.acquired is True
    assert res.first_doc_id == "doc-b"


def test_stale_holder_cannot_complete_or_release(index):
    dedup, _, clock = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    clock.now += LEASE + 1
    dedup.claim("h1", "doc-b", "s3://b/b", LEASE)
    assert dedup.complete("h1", "doc-a") is False
    assert dedup.release("h1", "doc-a") is False
    # The new holder's claim is untouched.
    res = dedup.check("h1")
    assert res.in_progress is True
    assert res.first_doc_id == "doc-b"


def test_release_after_complete_is_noop(index):
    dedup, _, _ = index
    dedup.claim("h1", "doc-a", "s3://b/a", LEASE)
    dedup.complete("h1", "doc-a")
    assert dedup.release("h1", "doc-a") is False
    assert dedup.check("h1").is_duplicate is True


def test_legacy_item_without_status_is_duplicate(index):
    dedup, ddb, _ = index
    ddb.put_item(
        TableName="dedup",
        Item={
            "content_hash": {"S": "h-legacy"},
            "doc_id": {"S": "doc-old"},
            "source_uri": {"S": "s3://b/old"},
            "expires_at": {"N": "9999999999"},
        },
    )
    res = dedup.check("h-legacy")
    assert res.is_duplicate is True
    assert res.first_doc_id == "doc-old"
    assert dedup.claim("h-legacy", "doc-new", "s3://b/new", LEASE).acquired is False
