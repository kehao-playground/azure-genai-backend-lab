# Testing Strategy

Placeholder — the pytest + behave strategy write-up. The test skeleton itself already lives in `tests/`:

- `tests/unit/` — fast, deterministic, no Azure dependency
- `tests/integration/` — fakes/mocks first; real Azure integration later behind env flags
- `tests/bdd/` — user-visible behavior and API contract scenarios (behave)
