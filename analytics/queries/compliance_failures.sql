-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
--
-- Documents that failed pack-driven compliance checks, most recent first.
-- The `compliance_result` column is stamped by the reconcile step using the
-- active industry pack (e.g. pile_capacity >= withstand_limit).
SELECT
    doc_id,
    doc_type,
    compliance_result,
    source_uri,
    ingested_at
FROM documents
WHERE compliance_result LIKE 'FAIL%'
ORDER BY ingested_at DESC
LIMIT 100;
