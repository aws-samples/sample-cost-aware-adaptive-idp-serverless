-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
--
-- Cost-savings summary: how many fields were resolved for free (born-digital /
-- spatial) vs. with paid services (Textract / Bedrock). This is the headline
-- "AI only when needed" metric, provable in SQL rather than asserted.
--
-- Run against the S3 Tables (Iceberg) namespace `idp`, table `documents`.
SELECT
    COUNT(*)                              AS documents,
    SUM(field_count)                      AS total_fields,
    SUM(free_field_count)                 AS free_fields,
    SUM(paid_field_count)                 AS paid_fields,
    ROUND(100.0 * SUM(free_field_count)
          / NULLIF(SUM(field_count), 0), 1) AS free_field_pct,
    ROUND(AVG(mean_confidence), 3)        AS avg_confidence
FROM documents;
