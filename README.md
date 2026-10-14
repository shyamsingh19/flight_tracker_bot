# Flight price alert

A single-user Telegram bot that watches domestic Indian fares through the Ignav API. For a walkthrough of using and configuring it, see [GUIDE.md](GUIDE.md).

## Files
| File | Role |
|---|---|
| `bot.py` | Entry point. Holds the Telegram commands and the tick, heartbeat, daily summary and nightly backup jobs |
| `checker.py` | Runs the anchor, buffer-scan, alternative and burst checks, plus active hours, expiry and the calibration replay |
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
`/track HYD DEL 2026-12-20 flex=5 target=4500 stops=0 airline=6E flight=6E2134 time=06-12 bags=1 every=120 force`
- Everything after the date is optional.
- `/track` refuses a route that would push projected spend past the hard stop, unless you add `force`.
- The first route you track becomes the 14-day calibration route: D is checked every 30 min, and the bot sends its verdict at day 14.

Other commands: `/list`, `/history id`, `/status`, `/checknow id [all]`, `/settarget [id] [4500|off]` (or 🎯 in My Alerts), `/pause id`, `/resume id`, `/delete id`, `/reload`.

To change a number, edit `config.toml` and send `/reload`. The bot replies with the new projected monthly cost and the keys that changed. If the file is invalid, it names the bad key and keeps the old config. `/reload` also re-reads `.env`, so a fixed `IGNAV_API_KEY` takes effect without a restart.

## Restore a backup
The bot sends `flightbot-YYYY-MM-DD.db` to you every night. To restore: `sudo systemctl stop flightbot`, copy the file to `flightbot.db` in the install directory, then `sudo systemctl start flightbot`.

## Tests
`python3 -m unittest` runs with no network, no Telegram and 0 Ignav calls.
