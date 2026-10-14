# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Single-user Telegram bot that watches domestic Indian fares via the Ignav API (the only data source). Flat layout, plain Python modules, no package. User-facing usage is in `README.md`.

## Commands

```bash
.venv/bin/python -m unittest                                   # all tests: no network, no Telegram, 0 Ignav calls
.venv/bin/python -m unittest test_flightbot.TestRules.test_grade  # one test
DRY_RUN=1 DB_PATH=dry.db .venv/bin/python bot.py               # run against fixtures/ (±10% jitter), no paid calls
.venv/bin/python validate.py [--cache-test]                    # Phase 1 live checks; SPENDS real Ignav calls, writes fixtures/
./install.sh                                                   # venv + systemd unit `flightbot` + tests
```

There is no linter or build step. Dependency: `python-telegram-bot[job-queue]` only; HTTP uses stdlib.

## Workflow

- Run `.venv/bin/python -m unittest` after every change; all must pass.
- Message text/formatting changes (help, /list, alerts) can't be verified by tests — say so, and check with the DRY_RUN bot in Telegram.

## Architecture

- `bot.py` — Telegram handlers plus JobQueue jobs (`tick`, heartbeat, daily summary, nightly backup). A `TypeHandler` in group -1 (`gate`) drops every update not from `ALLOWED_USER_ID`. The guided flow (`/start` menu → `/search` ConversationHandler → result buttons → track) uses callback-data prefixes `menu:`, `s:`, `r:`. Blocking work (`checker.run_due`, searches) runs via `asyncio.to_thread`.
- `checker.py` — scheduler core. `run_due(cfg, now)` is called every `tick_seconds`; for each active route it computes the latest due slot per job kind (`anchor` = date D, `scan` = D±flex buffer, `alt` = cheapest near-tied alternative date) from `config.job_slots`, stamps `last_<kind>_at` **before** running (failed slots are not retried), and returns message strings for the bot to send. Also handles expiry, good-deal bursts, and the 14-day calibration replay/report.
- `alerts.py` — `decide()` is pure: given price, history context and last alert state, it returns which rules fire. Message formatting lives here too.
- `providers.py` — Ignav client (`ignav_search` → `(offers, cache_hit, attempts)`, raises `ProviderError`). Under `DRY_RUN=1` it reads `fixtures/<O>_<D>.json`, falling back to `fixtures/sample_oneway.json`, which is **synthetic** (built from the OpenAPI schema).
- `budget.py` — every API attempt is recorded in a spend ledger (`budget.record`); `budget.blocked` enforces the hard stop. Any new code path that calls Ignav must check `blocked` first and `record` afterwards (see `bot.run_search`).
- `config.py` — `SCHEMA` validates `config.toml` (types, ranges, cross-checks) and holds the projected-cost formula (`calls_per_day`, `monthly_inr`). `/reload` re-reads both `config.toml` and `.env`; an invalid file is rejected and the old config is kept.
- `db.py` — thin SQLite (WAL) helpers (`q`, `one`, `run`, `insert`, `update_route`, `kv_get/kv_set`). Path comes from `DB_PATH` (default `flightbot.db`). Timestamps are stored as ISO UTC via `db.iso`/`db.parse`; scheduling is evaluated in `schedule.timezone` (IST).

## Conventions enforced by tests

- **Every tunable number lives in `config.toml`.** Adding a key means adding it to both `config.toml` and `config.SCHEMA`. `TestConfig` fails if the file and schema disagree, if a schema key is never referenced as `"key"` outside the SCHEMA block, or if default values (e.g. `0.002`, `115`, `750`, `1000`, `26`, `300`) appear as literals in `checker.py`, `alerts.py`, `budget.py` or `bot.py`.
- Code calls the provider through `checker.search` (not `providers.ignav_search` directly) so tests can swap in the `Fake` in `test_flightbot.py`. Tests point `db.PATH` at a temp file and pass explicit `now` datetimes; keep functions taking `cfg` and `now` as parameters rather than reading globals or the clock.
- Secrets (`TELEGRAM_TOKEN`, `IGNAV_API_KEY`, `ALLOWED_USER_ID`, optional `HEARTBEAT_URL`) live only in `.env`.
