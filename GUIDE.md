# Guide

Part 1 explains how to use the bot in Telegram. Part 2 explains how to tune it with `config.toml`. Setup and installation are in [README.md](README.md).

---

## Part 1: Using the bot

### The idea in one paragraph
You pick a route and a date **D** (for example HYD→DEL on 20 Dec). The bot checks the price of D through the day. Once a day it also checks the nearby dates (D±flex), and every few hours it re-checks the cheapest of them. It messages you only when something is worth acting on. Every check is a paid Ignav call (about ₹0.23), so the bot is built to check just often enough.

### Fastest path: the menu
Send `/start` to get four buttons:

| Button | What it does |
|---|---|
| 🔍 Flight Search | Asks for the departure city, the destination and the date, then shows the cheapest flights. **Costs one call.** |
| 🔔 My Alerts | Lists your tracked routes, with Delete, 🎯 Target and Pause/Resume buttons |
| 🎯 Set Target | Asks for a target price (and optional buffer); asks which route first if you track several |
| 📊 Status | Shows API health, money spent this month, free calls left and calibration progress |
| ❓ Help | Lists the commands |

**Search walkthrough:**
1. Type a city (`Hyderabad`, `bangalore`, `vizag`) or a 3-letter airport code (`HYD`).
2. Type the destination the same way.
3. Type the date in any of these forms: `6 november`, `Nov 6`, `06.11`, `6/11/2026`, `2026-11-06`, `tomorrow`. A date without a year means its next occurrence.
4. Results show the cheapest flights. Use the buttons under them:
   - **🔔 Track prices**: watch the cheapest fare for that route and date, whichever flight it is. The button turns into **✅ Tracking #N** (tap it to open My Alerts); searching the same route and date again shows it too, so you don't get duplicates. Delete or pause it from 🔔 My Alerts.
   - **✈️ Direct only / 🔀 All flights**: filter the list without making a new call. Tracking while the list is filtered sets `stops=0`.
   - **📅 Change date**: search the same route on another date (one new call).

Send `/cancel` to leave a search halfway through.

### Power path: `/track`
```
/track HYD DEL 2026-12-20 flex=5 target=4500 stops=0 airline=6E flight=6E2134 time=06-12 bags=1 every=120 force
```
Only the origin, destination and date are required.

| Option | Meaning | Default |
|---|---|---|
| `flex=N` | Also scan D−N to D+N once a day (0 = only D) | `flex_days` (5) |
| `target=4500` | Always alert at or below this price, whatever the history says | none |
| `stops=0` | Maximum number of stops (0–2) | `max_stops` (1) |
| `airline=6E` | Only this airline | any |
| `flight=6E2134` | Only this flight. Tracking stops after it departs | any |
| `time=06-12` | Departure between 06:00 and 12:00 | any |
| `bags=1` | Fares with at least this many checked bags | any |
| `every=120` | Check D every 120 min instead of the global value | `anchor_every_min` (60) |
| `force` | Track even if the projected spend would cross the hard stop | off |

### Managing routes
| Command | Effect |
|---|---|
| `/list` | All routes with their ids (`#3`) |
| `/history 3` | The last 20 prices seen for route 3 |
| `/settarget 3 4200` / `/settarget 3 off` | Change or remove the target price. `/settarget 3 8000 500` also sets this route's buffer (alert at ≤ ₹8,500). `/settarget` alone (or 🎯 Target in My Alerts) asks for the price; the id can be left out when you track one route |
| `/pause 3` / `/resume 3` | Stop or restart checks. A paused route costs nothing |
| `/delete 3` | Remove the route |
| `/checknow 3` | Check D right now (1 call) |
| `/checknow 3 all` | Check D and every date in D±flex. This is many calls, so the bot asks you to confirm by adding `yes` |
| `/status` | Health, spend and calibration |

A route expires on its own once its date passes, or, for a `flight=` route, once that flight departs.

### What the alerts mean
| Alert | When it fires |
|---|---|
| 🟢 **GOOD / GREAT DEAL** | D's price is in the cheapest 25% (good) or 10% (great) of the prices seen over the last 30 days. It needs at least 20 data points first, so expect no deal alerts in a new route's first day or so |
| 📉 **Drop** | D fell by at least ₹300 **and** 5% below its lowest price in the last 24 h |
| 💡 **Cheaper alternative** | A nearby date is cheaper than D by at least ₹300 or 5%, whichever is larger |
| 🎯 **Target** | The price is at or below your `target=` plus `[deal] target_buffer_inr` (₹300), so a fare just above target still pings you |

The same deal is not sent twice. The bot re-alerts only if the price falls by another ₹100 and 3%, or the deal upgrades from good to great. A good deal also triggers a **burst**: the bot checks D±5 straight away, so the alert shows nearby prices too.

Each alert includes a Google Flights link. **Book on the airline's website.** The bot only watches prices.

### Other messages you'll get
- **08:30 daily summary** of every route.
- **02:00 nightly backup**, a `.db` file you can restore from (see README).
- **Failure notice** if Ignav keeps failing (3 failures in a row, or 60 minutes of errors).
- **Budget warnings** at 50%, 80% and 100% of the monthly cap.
- **Calibration verdict on day 14.** The first route you track is checked every 30 minutes for 14 days. The bot then tells you whether checking every 60 or 120 minutes would have missed any drops, so you can choose `anchor_every_min` from evidence.

---

## Part 2: Configuring the bot

### Two files, two jobs
| File | Holds | Takes effect |
|---|---|---|
| `.env` | Secrets: `TELEGRAM_TOKEN`, `IGNAV_API_KEY`, `ALLOWED_USER_ID`, `HEARTBEAT_URL` | `/reload` (the Telegram token needs a restart) |
| `config.toml` | Every tunable number | `/reload` |

**Workflow:** edit `config.toml`, then send `/reload`. The bot replies with the new projected monthly cost and every key that changed (`deal.drop_abs_inr: 300 → 200`). If the file is invalid, the bot names the bad key and keeps the old config running, so a typo can't break it.

Messages from anyone other than `ALLOWED_USER_ID` are silently ignored. Get your id from @userinfobot.

### Where the money goes
Cost per route per day, using the defaults (active 08–23, ₹0.23 a call):

| Job | Calls/day | Controlled by |
|---|---|---|
| Anchor (date D) | 16 (hourly, 08:00–23:00) | `anchor_every_min`, `active_hours` |
| Buffer scan (D±flex) | 2 × flex = 10 | `flex_days` / `flex=`, `buffer_scan_every_h` |
| Alternative watch | 5 (every 3 h after 08:00) | `alt_watch_every_h` |
| **Total** | **31/day ≈ ₹214/month** | |

Other settings for comparison (monthly cost per route):

| Setup | ₹/month |
|---|---|
| defaults (flex 5, every 60) | 214 |
| `every=120` | 159 |
| `flex=3` | 186 |
| `flex=0` (D only, no alt watch) | 110 |
| `every=30` | 317 |

Dates more than 45 days away are checked 3× less often (`far_out_days`, `far_out_slowdown`), so they cost less until they get close.

With the default cap (₹750) and hard stop (90% = ₹675), you can run **about 3 routes at defaults**. When a fourth would cross the hard stop, `/track` refuses it. Choose one:
- Slow it down with `every=120` or a smaller `flex`.
- Raise `monthly_cap_inr`.
- Add `force` and accept that checks will stop for the rest of the month once ₹675 is reached.

Searches and `/checknow` count against the same budget. The first 1,000 Ignav calls are free, and `/status` shows how many are left.

### Recipes: "I want…"
| Goal | Change |
|---|---|
| Fewer, only-really-good alerts | `good_percentile = 15`, `great_percentile = 5`, `drop_abs_inr = 500` |
| More sensitive drop alerts | `drop_abs_inr = 200`, `drop_pct = 3` |
| No late-night checks | `active_hours = "08-21"` (also saves calls) |
| Spend less on all routes | `anchor_every_min = 120`, or `alt_watch_every_h = 0` to turn alternatives off |
| Scan nearby dates every 2 days | `buffer_scan_every_h = 48` |
| Wider flexibility | `flex_days = 7` (max is `max_flex_days`) |
| Deal alerts sooner on a new route | `min_samples = 10` (less reliable) |
| Business class | `cabin_class = "business"` |
| Skip the 14-day calibration | `[calibration] enabled = false` |
| Show more search results | `[ui] search_results_n = 8` |
| Quieter failure messages | `fail_notify_after = 5`, `fail_notify_cooldown_min = 180` |
| Card charges a different rate | `usd_inr_effective = 118`. This keeps the spend figures honest |

### Rules the validator enforces
- `active_hours` is `"HH-HH"` with start < end. `buffer_scan_at` must fall inside it.
- `buffer_scan_every_h` must be 0 or a multiple of 24.
- `flex_days` must be ≤ `max_flex_days`, and `great_percentile` must be ≤ `good_percentile`.
- Per-route `every=` can't go below `min_interval_min` (15).
- Unknown sections or keys are rejected, which catches typos.

### Practising without spending money
```bash
DRY_RUN=1 DB_PATH=dry.db .venv/bin/python bot.py
```
Every check reads `fixtures/` with prices jittered by ±10%, so alerts fire and you can try every command and config change at no cost. The separate `DB_PATH` keeps practice data out of your real ledger.

### For developers adding a setting
Add the key to **both** `config.toml` and `config.SCHEMA` in `config.py`, and read it as `cfg["section"]["key"]`. Never hard-code the number. The tests fail if the file and the schema disagree, or if a default value shows up as a literal in the code.
