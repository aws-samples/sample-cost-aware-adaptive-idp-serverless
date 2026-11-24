-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
--
-- Documents whose compliance verdict needs a human review, most recent first.
-- The reconcile step stamps NEEDS_REVIEW when a rule passes only on values the
-- model returned without OCR/bounding-box grounding, when a value does not
-- parse as one number, or when the Bedrock Guardrail intervened. The suffixes
-- [template=<id>@<state>] and [spot_check=...] name the template involved.
SELECT
    doc_id,
    doc_type,
    compliance_result,
    source_uri,
    ingested_at
FROM documents
WHERE compliance_result LIKE 'NEEDS_REVIEW%'
ORDER BY ingested_at DESC
LIMIT 100;
