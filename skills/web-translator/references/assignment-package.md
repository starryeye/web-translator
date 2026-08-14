# Immutable Assignment Package

The deterministic `prepare-assignments` command creates one UTF-8 JSON object at
`assignments/<zone-id>.json` before dispatch. The master writes only
`document-summary.txt` and `glossary.json`; it must not copy source records manually.
Each generated package is the only document-specific input a fresh translator agent
needs and has exactly these fields:

```json
{
  "schema_version": "1.0",
  "zone_id": "zone-001",
  "document_summary": "Concise whole-document purpose and terminology context.",
  "glossary": {"Retrieval Augmented Generation": "검색 증강 생성"},
  "targets": [],
  "context_before": [],
  "context_after": []
}
```

`targets` contains exactly the compact translation fields `id`, `semantic_type`,
`heading_path`, `source_text`, and `protected` for the records named by the zone's
`target_ids`, in source order. The two context arrays contain the same compact fields
only for records named by `context_before_ids` and `context_after_ids`. Do not include
DOM locators, target flags, or segment context indexes. Do not include unrelated segments,
chat history, review history, output from other zones, or duplicate contract prose.

Generate the package before spawning its agent and never modify it afterward. A translator
may read it and the separate `translator-contract.md`, but must write only its designated
`translations/<zone-id>.jsonl` file. The master retains ownership of the canonical
glossary, review evidence, DOM, and aggregate artifacts.
