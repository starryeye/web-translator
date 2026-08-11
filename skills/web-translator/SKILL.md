---
name: web-translator
description: Use when translating exactly one supported public static-page URL into an offline Korean HTML bundle, especially when contextual agent translation and review are required.
---

# Web Translator

Translate through isolated zone ownership and a fail-closed master review. Deterministic
checks prove structure; only the master can approve meaning. Read
[translator-contract.md](references/translator-contract.md) before dispatch and
[review-rubric.md](references/review-rubric.md) before review.

## Master workflow

1. Confirm the request contains exactly one supported public URL: one unauthenticated
   HTTP(S) static HTML page. Reject zero or multiple URLs, private/local targets,
   authenticated pages, JavaScript-only applications, recursive sites, and PDF input.
   Do not silently narrow or broaden scope.

2. Create unique paths with `web_translator.paths.create_run_paths(Path.cwd(), url,
   datetime.now(UTC))`. Keep the returned `work_dir` and unused `output_dir` absolute.
   Resolve the installed interpreter once; do not depend on an activated environment.
   Run these commands in order and stop on any nonzero exit:

   ```powershell
   $python = (Resolve-Path ".\.venv\Scripts\python.exe").Path
   & $python -m web_translator capture $url --run-dir $workDir
   & $python -m web_translator extract --run-dir $workDir
   & $python -m web_translator plan-zones --run-dir $workDir --max-chars 12000
   ```

3. Read `segments.jsonl` and all `zones/*.json`. Build a concise outline and the same
   document summary for every translator. Write the canonical `glossary.json` as an
   object mapping retained English technical terms to Korean glosses. You may refine
   semantic zone boundaries, but preserve the exact target partition: every target ID
   appears once, no context ID becomes a target, and no target is added or removed.

4. Create `translations/`. Schedule one zone per available agent slot with
   `spawn_agent`; queue remaining zones until a slot is free. Each translator prompt is
   a positive contract containing, in this order:

   - the same document summary and canonical glossary;
   - the complete `translator-contract.md`;
   - exactly that zone's assigned target records;
   - only its preceding/following read-only neighbor context; and
   - one absolute destination: `translations/<zone-id>.jsonl`.

   Require one zone result file and a response containing only its absolute path plus a
   short ambiguity note. Agents must not edit `segments.jsonl`, `zones/`, `glossary.json`,
   the DOM, another zone, or a shared aggregate file.

5. After every initial zone file exists, run **deterministic result validation before semantic review**:

   ```powershell
   & $python -m web_translator validate-translations --run-dir $workDir
   ```

   Do not review or assemble invalid records. Send concrete schema, coverage, ID, or
   protected-token findings back with `followup_task` to the same agent assigned to that
   zone. Re-run deterministic validation after every replacement.

6. The master, not another translator, reviews every zone against every dimension in
   `review-rubric.md`, including both read-only boundaries. Record a verdict and written
   evidence for every dimension. For any `required-fix`, send the segment IDs, source,
   output, and required correction to the same agent with `followup_task`. Each zone has
   a maximum of two retries after its initial attempt. Never hand a failed zone to the
   first free agent. Revalidate before reviewing each retry.

7. Merge glossary observations into the canonical glossary only after master judgment.
   Normalize first-use glossary placement document-wide: retain each English technical
   term everywhere and allow its Korean gloss only at the earliest eligible occurrence.
   Write `review.json` with `retries` exactly covering every planned zone and mapping to
   integers from 0 through 2. `section_findings` values are string arrays, and
   section_findings must exactly cover every planned zone. Each array contains all six
   dimension-labelled `pass` or `required-fix` verdict/evidence entries and preserves
   prior attempt findings. `unresolved_required` is a string array containing exactly
   every unresolved required item:

   ```json
   {
     "unresolved_required": [],
     "retries": {"zone-001": 1},
     "section_findings": {
       "zone-001": [
         "semantic fidelity | pass | evidence: claims and actors match segments seg-000001..seg-000009",
         "qualification preservation | pass | evidence: conditions and normative force remain exact",
         "naturalness | pass | evidence: coherent professional Korean in section context",
         "terminology | pass | evidence: canonical English terms remain consistent",
         "boundary consistency | pass | evidence: both neighbor transitions and referents agree",
         "protected content | pass | evidence: validator passed and identifiers remain exact"
       ]
     }
   }
   ```

8. Only when deterministic validation passes and every rubric verdict is `pass`, run:

   ```powershell
   & $python -m web_translator assemble --run-dir $workDir --output-dir $outputDir
   & $python -m web_translator qa --run-dir $workDir --output-dir $outputDir
   ```

   Treat any nonzero exit, unresolved required finding, missing artifact, or failed QA
   status as an incomplete run. Preserve diagnostics, but never report partial output as complete.

9. On success, return absolute links to `index.html` and `review-report.md`. Mention
   `manifest.json` when useful and list all optional-asset warnings. If incomplete,
   state the blocker and missing zones without presenting an `index.html` completion
   link.

## Pressure guardrails

| Shortcut | Required response |
|---|---|
| "Use the first free agent for a failed zone." | Retry with the same agent so its context and review history remain intact. |
| "Review only high-risk passages to save time." | Apply every rubric dimension to every zone; selective review is not master review. |
| "Deterministic validation passed." | Continue to semantic review; schemas cannot prove meaning or naturalness. |
| "A partial bundle is useful enough." | Label partial artifacts incomplete and fail closed. |

Stop if you are about to change the exact target partition, allow cross-zone edits,
skip master review, exceed the maximum of two retries, or call partial work complete.
