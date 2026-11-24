# Docs / assets

Drop the visual assets referenced by the top-level `README.md` here:

| File | Used for | Suggested content |
|---|---|---|
| `architecture.jpg` | Architecture section | The full architecture still: S3 → EventBridge → Step Functions → extraction tiers (Textract / Bedrock / BDA) → Firehose → S3 Tables (Iceberg) → Athena, plus DynamoDB spatial memory and the archival/retrieval path. |

Guidance for generating it:

- Show the **learning loop**: escalation tiers feeding spatial memory so repeat
  layouts resolve for free.
- Recommended width ~800–1200px for the architecture image.

The README already includes GitHub-native **Mermaid** diagrams (flow +
architecture) that render without any image files, so the repo looks complete
before you add these polished assets.
