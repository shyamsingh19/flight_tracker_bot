# Flight price alert

A Multi-user Telegram bot that watches domestic Indian fares through the Ignav API. For a walkthrough of using and configuring it, see [GUIDE.md](GUIDE.md).

## Files
| File | Role |
|---|---|
| `bot.py` | Entry point. Holds the Telegram commands and the tick, heartbeat, admin daily/weekly report and nightly backup jobs |
| `checker.py` | Runs the scheduled checks of each route's date, plus active hours and expiry |
| `alerts.py` | Alert rules (pure `decide()`) and the message text |
| `providers.py` | Ignav client. This is the only data source |
| `config.py` / `config.toml` | Schema, defaults, validation and the projected-cost formula / every tunable number |
| `budget.py` | Spend ledger, free-call counter, budget warnings and the hard stop |
| `db.py` | SQLite (WAL) |
| `validate.py` | Phase 1 live checks. Saves real responses to `fixtures/` |

## Phase 1: validate (about 10 free calls)
```bash
cp .env.example .env            # set IGNAV_API_KEY
python3 validate.py             # HYD to DEL/BOM/BLR/GOI/CCU, 30 days out; prints the carriers found
python3 validate.py --cache-test  # same date at 0/15/30/60 min; prints cache_hit
```
Then hand-check 5 prices on the airline sites (they should be within ±3%), and email Ignav to ask whether its data is licensed or scraped.
`fixtures/sample_oneway.json` is **synthetic**: it was built from Ignav's OpenAPI schema, not from a real response. `validate.py` saves real responses next to it.

## Run
```bash
./install.sh                    # venv, systemd unit, runs the tests
sudo systemctl start flightbot
journalctl -u flightbot -f      # JSON-line logs
```
To try it without spending calls, run `DRY_RUN=1 DB_PATH=dry.db .venv/bin/python bot.py`. This answers every check from `fixtures/` with prices jittered by ±10%. Use a separate `DB_PATH` so these calls don't count against your free 1,000.

## Use
Send `/start` to the bot. Commands, alerts and every config key are explained in [GUIDE.md](GUIDE.md). To change a number, edit `config.toml` and send `/reload`; an invalid file is rejected and the old config kept. `/reload` also re-reads `.env`.

## Restore a backup
Every night the bot saves `backups/flightbot-YYYY-MM-DD.db` (the newest `backup_keep` are kept) and sends the same file to the admin. The Telegram copy is the one that survives losing the server. To restore: `sudo systemctl stop flightbot`, copy the file to `flightbot.db` in the install directory, then `sudo systemctl start flightbot`.

## Tests
`python3 -m unittest` runs with no network, no Telegram and 0 Ignav calls.
