-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
-- SPDX-License-Identifier: MIT-0
--
-- Field counts per extraction method, unnested from the per-document
-- method summary JSON. Shows the tier mix actually used in production:
-- free methods (born_digital, spatial) should dominate.
--
-- `method_summary_json` is a JSON object like {"born_digital": 8, "llm": 1}.
SELECT
    method,
    SUM(CAST(field_count AS INTEGER)) AS fields
FROM documents
CROSS JOIN UNNEST(
    CAST(json_parse(method_summary_json) AS MAP(VARCHAR, INTEGER))
) AS t(method, field_count)
GROUP BY method
ORDER BY fields DESC;
