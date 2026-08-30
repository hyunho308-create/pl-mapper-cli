# Upstream synchronization

The normalization engine in this repository is distributed from
[`hyunho308-create/P-L-Agent`](https://github.com/hyunho308-create/P-L-Agent).
`P-L-Agent` is the source of truth for shared production behavior.

The upstream checkout contains:

- `scripts/cli_sync_manifest.json`, which defines the shared package boundary
  and shared tests;
- `scripts/sync_cli_repo.py --check`, which reports drift; and
- `scripts/sync_cli_repo.py --apply`, which updates this repository.

This repository intentionally owns its README, getting-started guide, catalog,
workflow diagram, package version, direct OpenAI dependency, and CLI-only tests.
Web server code, deployment controls, and internal corpus-reporting utilities
are not distributed here.

Shared engine files should be changed in `P-L-Agent` first and synchronized
here. Avoid making independent fixes to shared files in both repositories.
