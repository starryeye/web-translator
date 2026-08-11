# Master Review Rubric

Review every zone and both read-only neighbor boundaries. Give every dimension exactly
one verdict—`pass` or `required-fix`—with written evidence. A summary such as “looks
good” is not evidence. A `required-fix` names the segment ID, quotes or precisely
paraphrases the source and translation, and states the correction required.

| Dimension | `pass` evidence | `required-fix` evidence |
|---|---|---|
| semantic fidelity | Explain why claims, actors, actions, references, and logic retain the source meaning. | Identify an addition, omission, inversion, mistranslation, or broken relationship. |
| qualification preservation | Account for conditions, scope, negation, modality, exceptions, and normative force. | Identify a weakened, strengthened, missing, or invented qualification. |
| naturalness | Confirm the Korean reads as coherent professional prose in section context. | Identify mechanical word order, fragments, ambiguity, or unnatural repetition and prescribe a natural correction. |
| terminology | Confirm canonical English technical terms remain visible and glossary usage is consistent. | Identify Korean-only replacement, inconsistent English, premature glossing, or a glossary conflict. |
| boundary consistency | Compare the zone opening/closing with read-only neighbors for referents, tone, transitions, and term choices. | Identify a cross-zone contradiction, dangling reference, duplicated transition, or terminology drift. |
| protected content | Confirm exact placeholders, identifiers, URLs, commands, RFC references, and normative keywords. | Identify any changed, missing, duplicated, or newly introduced protected content. |

Record findings under the zone ID and preserve findings from all attempts. A zone passes
only when all six dimensions pass. Any `required-fix` triggers a same-agent retry, then
deterministic validation before another semantic review. After two retries, leave the
finding unresolved and fail the run.
