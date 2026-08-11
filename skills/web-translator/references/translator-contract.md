# Translator Contract

Produce natural contextual Korean after understanding the section's role in the whole
document. Preserve meaning, logical relationships, references, tone, and every
qualification; do not perform mechanical sentence-by-sentence substitution.

## Terminology and protected content

- Retain English technical terms exactly and consistently. Do not replace them with
  Korean-only equivalents.
- Use the supplied glossary as canonical. Put proposed additions or concerns only in the
  separate `glossary_observations` object; do not revise the shared glossary.
- Leave document-wide first-use Korean gloss placement to the master.
- Enforce exact protected-token preservation: every placeholder such as
  `⟦WT:000000⟧` appears with the exact spelling and multiplicity supplied for its segment.
- Preserve protocol identifiers, product names, commands, URLs, RFC references, and
  normative force such as `MUST`, `SHOULD`, and `MAY`.

## Ownership and completeness

Return the exact assigned IDs once each and in source order. Neighbor records are
read-only context and must never appear in the result. Write only the assigned zone's
result file.

Do not add explanations, examples, claims, markup, or content absent from the source.
Do not omit caveats, conditions, negation, modality, references, or meaningful detail.
Use `notes` only when genuine ambiguity affects master review.

## JSON Lines output

Write UTF-8 JSON Lines: one complete JSON object per line, no array, Markdown fence, or
surrounding prose. Every object has exactly this shape:

```json
{"segment_id":"seg-000001","text":"Security Token Service(보안 토큰 서비스)는 ⟦WT:000000⟧ 요청을 검증해야 합니다.","notes":null,"glossary_observations":{"Security Token Service":"영어 용어를 유지하고 문서 최초 사용에만 한국어 풀이를 제안합니다."}}
{"segment_id":"seg-000002","text":"Client는 해당 응답을 다음 교환에 사용합니다.","notes":null,"glossary_observations":{}}
```

Before returning the file path, verify exact assigned IDs, JSON decoding, natural
contextual Korean, English technical terms, and protected placeholders.
