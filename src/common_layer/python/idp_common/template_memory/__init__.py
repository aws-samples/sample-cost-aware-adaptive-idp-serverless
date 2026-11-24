# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spatial field-location memory, built for the cost-aware IDP pipeline.

Records WHERE fields appear on document templates and uses that knowledge to
identify document types and locate fields without paid AI. This is an
independent implementation; the core idea — learning field locations from a
layout so repeat documents resolve spatially instead of re-running extraction
— is credited to the open-source ``sample-textract-field-memory`` project
(aws-samples/sample-textract-field-memory, MIT-0; see ``NOTICE``). No code was
ported from it. This implementation is scoped to what the pipeline needs and
uses a pluggable persistence backend (local files for tests, DynamoDB
on-demand in production).
"""

from idp_common.template_memory.facade import TemplateMemory
from idp_common.template_memory.identifier import TemplateIdentifier, TemplateMatch
from idp_common.template_memory.matcher import FieldMatch, SpatialMatcher
from idp_common.template_memory.models import (
    TRUST_PROVISIONAL,
    TRUST_TRUSTED,
    FieldLocationMap,
    FieldRegion,
)
from idp_common.template_memory.spot_check import compare, select_for_spot_check
from idp_common.template_memory.store import (
    DynamoDBTemplateStore,
    FileTemplateStore,
    TemplateStore,
)

__all__ = [
    "TemplateMemory",
    "TemplateIdentifier",
    "TemplateMatch",
    "SpatialMatcher",
    "FieldMatch",
    "FieldLocationMap",
    "FieldRegion",
    "TRUST_PROVISIONAL",
    "TRUST_TRUSTED",
    "select_for_spot_check",
    "compare",
    "TemplateStore",
    "FileTemplateStore",
    "DynamoDBTemplateStore",
]
