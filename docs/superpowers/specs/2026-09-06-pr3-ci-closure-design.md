# PR 3 CI Closure Design

## Goal

Make pull request #3 pass real pull-request CI while preserving the invariant that production API and worker runtime authority is PostgreSQL-only.

## Non-negotiable authority boundary

- Environment-driven production composition must reject SQLite and missing PostgreSQL domain/control DSNs.
- No code path may infer SQLite because a PostgreSQL DSN is absent.
- `DatabaseMode.SQLITE` remains only a rejected legacy sentinel.
- Tests may use explicit, already-constructed in-memory or SQLite-backed repositories only through test composition/dependency injection; that capability must not be reachable from production environment configuration.

## Approaches considered

1. **Explicit test composition (selected).** Add or restore test-only fixture builders that inject repositories into lower-level API/service constructors. This preserves fast unit tests and the production invariant.
2. Provision PostgreSQL for every test. This is authoritative but would convert a large unit suite into slow integration tests and require broad fixture migration.
3. Restore SQLite fallback behind an environment flag. Rejected because it reintroduces dual runtime authority.

## Implementation slices

1. Introduce the smallest explicit injected composition boundary needed by existing unit tests. Update obsolete factory tests to assert SQLite rejection. Do not weaken production builders.
2. Install the PostgreSQL extra in Python CI only for tests that instantiate PostgreSQL adapters. Mark bubblewrap execution tests as capability-dependent while keeping sandbox-policy tests mandatory.
3. Resolve the independent Agent turn event-sequence regression after the dominant construction failures are removed.
4. Replace or annotate the three scanner findings using synthetic, non-secret fixtures and narrow line-level allow markers only where documentation must show a literal shape.
5. Align Playwright selectors and mocked fixtures with the current UI contract, including text-scoped alert selection.
6. Regenerate and commit `src/pilot107/web/static` only after source/test changes stabilize.

## Verification

Run targeted red/green tests for every production change, then:

```sh
uv sync --locked --extra dev --extra api
uv run ruff check src tests scripts
uv run mypy src
uv run pytest -q
npm ci
npm run typecheck
npm test -- --run
npm run test:ui
npm run build
git diff --exit-code -- src/pilot107/web/static
uv run python scripts/scan-tracked-secrets.py
uv run pip-audit --progress-spinner off
npm audit --omit=dev --audit-level=high
sh simulator/compose/scripts/check-compose-config.sh
```

The PR is merge-ready only when all five GitHub Actions jobs pass on the current PR merge commit.
