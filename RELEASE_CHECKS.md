# Release checks

Copperline 0.1.0 repository baseline.

## Size

- production Python files: 34
- production Python LOC: 7970
- test Python files: 14
- test Python LOC: 1256
- Markdown files: 12
- Markdown LOC: 784

## Executed validation

- `python -m compileall -q src/copperline`: PASS
- `python -m unittest discover -s tests -t .`: PASS, 117/117 tests
- editable package build/install with `pip install -e . --no-build-isolation --no-deps`: PASS
- wheel build with `pip wheel . --no-build-isolation --no-deps`: PASS
- installed `copperline --help`: PASS
- real local HTTP API requests: covered by end-to-end tests
- file-backed SQLite close/reopen business lifecycle: covered by end-to-end tests
- SQLite online backup + archive verification: covered by integration tests

## Repository hygiene

- `.git` directory: intentionally absent
- virtual environment: absent
- `node_modules`: not applicable
- build/egg-info caches: removed from repository bundle
- runtime third-party dependencies: none

This file records checks actually executed in the authoring environment. It is not a
claim of statutory accounting or tax certification.
