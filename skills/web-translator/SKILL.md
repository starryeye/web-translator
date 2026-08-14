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
   & $python -m web_translator plan-zones --run-dir $workDir --max-chars 12000 --target-zones 3
   ```

   Three target zones match the normal three translator slots and minimize the slowest
   assignment by estimated source size. If the active collaboration surface explicitly
   advertises a different translator-slot limit, use that positive limit for
   `--target-zones` instead. `--max-chars` remains a hard bound.

3. Read `segments.jsonl` and all `zones/*.json`. Build a concise outline and the same
   document summary for every translator (the same document summary is embedded in each
   package). Write the canonical `glossary.json` as an object mapping retained English
   technical terms to Korean glosses. Preserve the exact
   target partition: every target ID appears once, no context ID becomes a target, and
   no target is added or removed.

   Write the concise summary to `document-summary.txt`, then build one immutable
   assignment package per zone using the deterministic packager:

   ```powershell
   & $python -m web_translator prepare-assignments --run-dir $workDir
   ```

   The generated packages follow
   [assignment-package.md](references/assignment-package.md). Each contains the concise
   summary, canonical glossary, exactly that zone's compact target translation fields,
   and only its bounded read-only neighbor context fields. Do not paste source records
   into agent prompts.

4. Create `translations/`. Schedule one zone per available agent slot with
   `spawn_agent` and `fork_turns="none"`; use `reasoning_effort="medium"` for the
   translation draft. Queue remaining zones until a slot is free. Every short,
   self-contained prompt supplies only:

   - the absolute immutable assignment package path;
   - the absolute `translator-contract.md` path, which the agent must read completely;
   - one absolute destination: `translations/<zone-id>.jsonl`; and
   - the requirement to return only that path plus a short ambiguity note.

   Require one zone result file from each fresh agent identity. Agents must not
   edit the immutable assignment package, `segments.jsonl`, `zones/`, `glossary.json`,
   the DOM, another zone, or a shared aggregate file. The assignment package plus these
   paths is the complete positive contract; never rely on inherited conversation context.

5. As soon as each zone file exists, run **deterministic result validation before
   semantic review** for that zone, even while other translators are still running:

   ```powershell
   & $python -m web_translator validate-translations --run-dir $workDir --zone-id zone-001
   ```

   Substitute the completed zone ID. Do not review or assemble invalid records. Send
   concrete schema, coverage, ID, or protected-token findings back with `followup_task`
   to the same agent assigned to that zone. Re-run deterministic validation after every
   replacement.

6. The master, not another translator, owns semantic approval. The workflow must review
   completed zones while other translators are still running. Review every zone against
   every dimension in `review-rubric.md`, including both read-only boundaries. Record concise written
   evidence for every dimension. Deterministic validation may supply the protected-content
   evidence; the master still judges meaning, qualifications, naturalness, terminology,
   and boundaries. For any `required-fix`, send only the affected segment IDs, source,
   output, and required correction to the same agent with `followup_task`. Each zone has
   a maximum of two retries after its initial attempt. Never hand a failed zone to the
   first free agent. Revalidate before reviewing each retry.

7. After every zone has a valid result, run the aggregate validator once to prove exact
   document-wide coverage before assembly:

   ```powershell
   & $python -m web_translator validate-translations --run-dir $workDir
   ```

   Merge glossary observations into the canonical glossary only after master judgment.
   Normalize first-use glossary placement document-wide: retain each English technical
   term everywhere and allow its Korean gloss only at the earliest eligible occurrence.
   Write `review.json` with `retries` exactly covering every planned zone and mapping to
   integers from 0 through 2. section_findings must exactly cover every planned zone.
   Each value is an array of six objects with exactly `dimension`, `verdict`, and
   non-empty `evidence`; use each canonical dimension exactly once. A verdict is only
   `pass` or `required-fix`. `unresolved_required` is a sorted, unique string array
   containing exactly every `zone-ID:dimension` whose verdict is `required-fix`:

   ```json
   {
     "unresolved_required": [],
     "retries": {"zone-001": 1},
     "section_findings": {
       "zone-001": [
         {"dimension":"semantic_fidelity","verdict":"pass","evidence":"Claims and actors match segments seg-000001..seg-000009."},
         {"dimension":"qualification_preservation","verdict":"pass","evidence":"Conditions and normative force remain exact."},
         {"dimension":"naturalness","verdict":"pass","evidence":"Korean is coherent professional prose in section context."},
         {"dimension":"terminology","verdict":"pass","evidence":"Canonical English terms remain consistent."},
         {"dimension":"boundary_consistency","verdict":"pass","evidence":"Both neighbor transitions and referents agree."},
         {"dimension":"protected_content","verdict":"pass","evidence":"Validator passed and identifiers remain exact."}
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
