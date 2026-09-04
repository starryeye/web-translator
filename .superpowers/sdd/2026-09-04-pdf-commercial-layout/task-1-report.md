# Task 1 report: semantic roles and schema 1.1 compatibility

## Implementation

- Added the public `PdfSemanticRole` literal vocabulary and `PDF_DOCUMENT_SCHEMA_VERSION = "1.1"`.
- Added `PdfBlock.semantic_role`, defaulting to `"body"`, with serialization and strict vocabulary validation.
- Added `upgrade_pdf_document_v1`, and made `PdfDocument.from_dict` upgrade root schema `1.0` documents to `1.1` while rejecting other versions.
- Updated PDF extraction output to emit schema `1.1`.
- Updated shared fixtures and model tests for the new contract.

## RED evidence

Command:

```text
PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_models_paths.py -q
```

Result: 2 expected failures (unsupported semantic role and schema 1.0 upgrade), 38 passed.

## GREEN evidence

Command:

```text
git diff --check && PYTHONPATH=src ../../.venv/bin/python -m pytest tests/test_pdf_models_paths.py tests/test_pdf_extract.py -q
```

Result: `174 passed in 27.63s`.

## Full-suite evidence

Command:

```text
PYTHONPATH=src ../../.venv/bin/python -m pytest -q
```

Result: `998 passed, 9 skipped, 19 failed, 2 errors, 2 deselected in 83.86s`.

The failures/errors are sandbox-only browser/localhost transport checks: Chromium reports `[Errno 1] Operation not permitted`, and tests that bind `127.0.0.1` fail with `PermissionError: [Errno 1] Operation not permitted`.

Non-browser/PDF-focused command:

```text
PYTHONPATH=src ../../.venv/bin/python -m pytest -q --ignore=tests/test_qa.py --ignore=tests/test_pipeline.py -k 'not test_http_pdf_acquire_and_extract_uses_real_transport'
```

Result: `923 passed, 9 skipped, 3 deselected in 84.53s`.

## Changed files

- `src/web_translator/pdf_models.py`
- `src/web_translator/pdf_extract.py`
- `tests/pdf_fixtures.py`
- `tests/test_pdf_models_paths.py`

## Self-review

The compatibility branch is limited to root schema `1.0`; schema `1.1` is validated strictly, and unsupported versions are rejected. Legacy blocks receive only the required default body role. Source, review, and other contract schema versions remain on their existing validator.

## Concerns

Full suite browser and localhost failures are environmental sandbox restrictions, not contract failures. No tests were weakened.

## Commit

`a8c3ed6 feat: add PDF semantic role contract`
