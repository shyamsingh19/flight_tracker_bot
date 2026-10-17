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
did- Keep this file current: whenever a change or decision alters behaviour, conventions, commands or architecture described here (or adds something future work needs to know), update CLAUDE.md in the same change. Record the decision and its reason, not a changelog.
- State that only matters on one machine (live DB, running service, pending local steps, known local test failures) goes in the gitignored `CLAUDE.local.md`, kept current the same way; not here.
- Message text/formatting changes (help, /list, alerts) can't be verified by tests — say so, and check with the DRY_RUN bot in Telegram.

## Architecture

- `bot.py` — Telegram handlers plus JobQueue jobs (`tick`, heartbeat, daily summary, nightly backup). A `TypeHandler` in group -1 (`gate`) controls access: `ALLOWED_USER_ID` (the admin) gets everything; `ALERT_USER_IDS` get every feature except the commands and buttons in `ADMIN_ONLY` (`/reload`, `/status`, `menu:status`, i.e. server health, spend and config), which get an "Admin only" reply; everyone else is dropped silently. Routes are per user: `routes.owner_id` is set to whoever tracked it, and every lookup from a handler goes through `own_route`/`get_route` or filters by `owner_id`, so users only see and change their own routes (admin included). Old rows with no owner are given to the admin at startup (`bot.main`). Scheduled messages go through `send`/`recipients`: route messages (`alerts.Flight`, built with `alerts.for_owner`: price alerts, expiry) go only to the route's owner; everything else (failures, summary, spend, backups) goes to the admin only, unless `[monitoring] admin_notices = false`. The guided flow (`/start` menu → `/search` ConversationHandler → result buttons → track) uses callback-data prefixes `menu:`, `s:`, `r:` (status), `t:` (target), `v:` (route view). Users never see route ids: routes are shown as `alerts.route_name` ("HYD → DEL · Sun 20 Dec"). Commands that take a route (`PICKABLE` + `/settarget`) call `pick()`, which returns the route when `ctx.args[0]` is one of the user's ids, else replies with one button per route carrying `p:<cmd>:<id>:<other typed words>`; `pick_button` turns that back into `ctx.args` and re-runs the command. Typed ids still work but are undocumented. Blocking work (`checker.run_due`, searches) runs via `asyncio.to_thread`.
- `checker.py` — scheduler core. **A route watches exactly one date D**; there are no nearby/flex dates, alternative-date watch, bursts or CHEAPER DATE alerts (removed 2026-10-17 because they made scheduling, cost and alerts much more complex; a user who is flexible tracks each day as its own route). `run_due(cfg, now)` is called every `tick_seconds`; for each active route it finds today's latest due slot from `config.job_slots`, stamps `last_anchor_at` **before** running (failed slots are not retried), and returns message strings for the bot to send. Every scheduled check (and `/checknow`) is one `scan()`: one fetch of D, then every rule evaluated together → **at most one alert message per scan**. The rule context (lowest seen, baseline) is read *before* the new price is saved so it is compared against the past only. Re-checks and BOOK BY also live here. Also handles expiry.
- `alerts.py` — its module docstring is the alert spec (rules, re-alert state, re-check); keep code and docstring in sync. `decide()` is pure: given one price, its context and `db.alert_states`, it returns `(fired, clear)`. Message formatting lives here too.
- `providers.py` — Ignav client (`ignav_search` → `(offers, cache_hit, attempts)`, raises `ProviderError`). Under `DRY_RUN=1` it reads `fixtures/<O>_<D>.json`, falling back to `fixtures/sample_oneway.json`, which is **synthetic** (built from the OpenAPI schema).
- `budget.py` — every API attempt is recorded in a spend ledger (`budget.record`); `budget.blocked` enforces the hard stop. Any new code path that calls Ignav must check `blocked` first and `record` afterwards (see `bot.run_search`).
- `config.py` — `SCHEMA` validates `config.toml` (types, ranges, cross-checks) and holds the projected-cost formula (`calls_per_day`, `monthly_inr`). `/reload` re-reads both `config.toml` and `.env`; an invalid file is rejected and the old config is kept.
- `db.py` — thin SQLite (WAL) helpers (`q`, `one`, `run`, `insert`, `update_route`, `kv_get/kv_set`) plus observation/alert-state helpers; its docstring documents the tables. Rules read only `valid=1` observations; only rows with a `baseline_slot` (one per scheduled slot) form the GOOD/GREAT baseline. Alert lines and re-alert state are written by `bot.send` via `db.record_message` **after** the Telegram send succeeds (the `alerts.Flight` carries them in `.record`), so a failed send doesn't silence the next alert; tests do the same with `sent()`. `db.init` adds `ADDED_COLUMNS` and drops `DROPPED_COLUMNS` (the old flex columns; `flex_days` was NOT NULL so it must go) idempotently on every start. Path comes from `DB_PATH` (default `flightbot.db`). Timestamps are stored as ISO UTC via `db.iso`/`db.parse`; scheduling is evaluated in `schedule.timezone` (IST).

## Conventions enforced by tests

- **Every tunable number lives in `config.toml`.** Adding a key means adding it to both `config.toml` and `config.SCHEMA`. `TestConfig` fails if the file and schema disagree, if a schema key is never referenced as `"key"` outside the SCHEMA block, or if default values (e.g. `0.002`, `115`, `750`, `1000`, `26`, `300`) appear as literals in `checker.py`, `alerts.py`, `budget.py` or `bot.py`.
- Code calls the provider through `checker.search` (not `providers.ignav_search` directly) so tests can swap in the `Fake` in `test_flightbot.py`. Tests point `db.PATH` at a temp file and pass explicit `now` datetimes; keep functions taking `cfg` and `now` as parameters rather than reading globals or the clock.
- Secrets (`TELEGRAM_TOKEN`, `IGNAV_API_KEY`, `ALLOWED_USER_ID`, optional `ALERT_USER_IDS` and `HEARTBEAT_URL`) live only in `.env`.
