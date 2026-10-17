# Guide

Part 1 explains how to use the bot in Telegram. Part 2 explains how to tune it with `config.toml`. Setup, installation and restoring a backup are in [README.md](README.md).

---

## Part 1: Using the bot

### The idea in one paragraph
You pick a route and a date **D** (for example HYD→DEL on 20 Dec). The bot checks the price of D, and only D, through the day. If you could also fly on another day, track that day as its own route. It messages you only when something is worth acting on. Every check is a paid Ignav call (about ₹0.23), so the bot is built to check just often enough.

### Fastest path: the menu
Send `/start` to get the menu:

| Button | What it does |
|---|---|
| 🔍 Flight Search | Asks for the departure city, the destination and the date, then shows the cheapest flights. **Costs one call.** |
| 🔔 My Alerts | Lists your tracked routes. Tap a route for its buttons: 🔄 Check now, 🎯 Target, 📜 History, ⏸ Pause / ▶️ Resume, ❌ Delete |
| 🎯 Set Target | Asks which route, then the target price (and optional buffer) |
| 📊 Status | API health, money spent this month and free calls left (admin only) |
| ❓ Help | Lists the commands |

**Search walkthrough:**
1. Type a city (`Hyderabad`, `bangalore`, `vizag`) or a 3-letter airport code (`HYD`).
2. Type the destination the same way.
3. Type the date in any of these forms: `6 november`, `Nov 6`, `06.11`, `6/11/2026`, `2026-11-06`, `tomorrow`. A date without a year means its next occurrence.
4. Results show the cheapest flights. Use the buttons under them:
   - **🔔 Track prices**: watch the cheapest fare for that route and date, whichever flight it is. The button turns into **✅ Tracking (see My Alerts)**, and searching the same route and date again shows it too, so you don't get duplicates.
   - **✈️ Direct only / 🔀 All flights**: filter the list without making a new call. Tracking while the list is filtered tracks direct flights only.
   - **📅 Change date**: search the same route on another date (one new call).

Send `/cancel` to leave a search halfway through.

### Power path: `/track`
```
/track HYD DEL 2026-12-20 target=4500 stops=0 airline=6E flight=6E2134 time=06-12 every=120 force
```
Only the origin, destination and date are required.

| Option | Meaning | Default |
|---|---|---|
| `target=4500` | Alert at or below this price (plus the buffer), whatever the history says | none |
| `stops=0` | Maximum number of stops (0–2) | `max_stops` (1) |
| `airline=6E` | Only this airline | any |
| `flight=6E2134` | Only this flight. Tracking stops after it departs | any |
| `time=06-12` | Departure between 06:00 and 12:00 | any |
| `every=120` | Check D every 120 min instead of the global value | `anchor_every_min` (30) |
| `force` | Track even if the projected spend would cross the hard stop | off |

### Commands
| Command | Effect |
|---|---|
| `/list` | Your routes with their latest price (same as 🔔 My Alerts) |
| `/history` | Every check of a route: time, auto/manual/re-check, price or error, and which alert it sent |
| `/checknow` | Check a route right now (1 call; `checknow_per_user_day` per user per day) |
| `/settarget` | Set a target: `/settarget 4200`, `/settarget 8000 500` (alert at ≤ ₹8,500) or `/settarget off` |
| `/status`, `/reload` | Admin only: health and spend; re-read the config |

Commands that act on a route reply with one button per route (for example "HYD → DEL · Sun 20 Dec"), so you never type an id. Pause, resume and delete are buttons on each route in 🔔 My Alerts. A paused route costs nothing.

A route expires on its own once its date passes, or, for a `flight=` route, once that flight departs.

### What the alerts mean
| Alert | When it fires |
|---|---|
| 📉 **New low** | D is below the lowest price the bot has recorded for it. It waits for 20 recorded prices first (`min_samples`), so a new route's first noisy prices don't each ping you |
| 🎯 **Target** | The price is at or below your target plus the buffer (₹300 by default), so a fare just above target still pings you |
| ⏰ **Book by** | Once, 14 days before D: today's price and the lowest seen, because fares usually rise near departure |

One check sends at most one message. The same alert is not repeated unless the price falls by at least another ₹100 or 3% (whichever is larger), or a near-target fare reaches the target. If the deal goes away and comes back, it alerts again.

Before alerting a cached price, or a price more than 30% below the lowest seen, the bot searches once more to confirm it. If that second search fails, the alert is marked ⚠️ unverified.

Each alert includes a Google Flights link. **Book on the airline's website.** The bot only watches prices.

### Other messages
Every user gets alerts for their own routes, and a notice when one expires. The admin also gets:
- the **08:30 daily summary** of every route,
- the **02:00 nightly backup** (`.db` file),
- a **failure notice** if Ignav keeps failing (3 failures in a row, or 60 minutes of errors),
- **budget warnings** at 50%, 80% and 100% of the monthly cap.

---

## Part 2: Configuring the bot

### Two files, two jobs
| File | Holds | Takes effect |
|---|---|---|
| `.env` | Secrets: `TELEGRAM_TOKEN`, `IGNAV_API_KEY`, `ALLOWED_USER_ID`, `ALERT_USER_IDS`, `HEARTBEAT_URL` | `/reload` (the Telegram token needs a restart) |
| `config.toml` | Every tunable number | `/reload` |

**Workflow:** edit `config.toml`, then send `/reload`. The bot replies with the new projected monthly cost and every key that changed (`deal.realert_abs_inr: 100 → 200`). If the file is invalid, the bot names the bad key and keeps the old config running, so a typo can't break it.

**Users.** `ALLOWED_USER_ID` is the admin. Users listed in `ALERT_USER_IDS` (comma-separated) can use everything except `/status` and `/reload`. Each user sees and changes only their own routes, and must press Start in the bot once. Messages from anyone else are silently ignored. Get ids from @userinfobot. `[monitoring] admin_notices = false` stops the admin-only messages (the nightly backup is still saved on the server).

### Where the money goes
With the defaults (active 08–23, a check every 30 min, ₹0.23 a call), one route costs **31 calls a day ≈ ₹214/month**.

| Setup | Calls/day | ₹/month per route |
|---|---|---|
| every 30 min (default) | 31 | 214 |
| `every=60` | 16 | 110 |
| `every=120` | 8 | 55 |

Dates more than 45 days away are checked 3× less often (`far_out_days`, `far_out_slowdown`), so they cost less until they get close.

With the default cap (₹750) and hard stop (90% = ₹675), you can run about 3 routes at the default rate. When the next one would cross the hard stop, `/track` refuses it. Choose one:
- Slow it down with `every=120`.
- Raise `monthly_cap_inr`.
- Add `force` and accept that checks will stop for the rest of the month once ₹675 is reached.

Searches, `/checknow` and re-checks count against the same budget. The first 1,000 Ignav calls are free, and `/status` shows how many are left.

### Recipes: "I want…"
| Goal | Change |
|---|---|
| Fewer repeat alerts | `realert_abs_inr = 300`, `realert_pct = 5` |
| New-low alerts sooner on a new route | `min_samples = 10` (noisier) |
| No late-night checks | `active_hours = "08-21"` (also saves calls) |
| Spend less on all routes | `anchor_every_min = 60` |
| A wider target margin | `target_buffer_inr = 500` |
| Business class | `cabin_class = "business"` |
| Show more search results | `[ui] search_results_n = 8` |
| Quieter failure messages | `fail_notify_after = 5`, `fail_notify_cooldown_min = 180` |
| Keep more backups on disk | `backup_keep = 30` |
| Card charges a different rate | `usd_inr_effective = 118`. This keeps the spend figures honest |

### Rules the validator enforces
- Every key has a type and a range; unknown sections or keys are rejected, which catches typos.
- `active_hours` is `"HH-HH"` with start < end.
- Per-route `every=` can't go below `min_interval_min` (15).

### Practising without spending money
```bash
DRY_RUN=1 DB_PATH=dry.db .venv/bin/python bot.py
```
Every check reads `fixtures/` with prices jittered by ±10%, so alerts fire and you can try every command and config change at no cost. The separate `DB_PATH` keeps practice data out of your real ledger.

### For developers adding a setting
Add the key to **both** `config.toml` and `config.SCHEMA` in `config.py`, and read it as `cfg["section"]["key"]`. Never hard-code the number. The tests fail if the file and the schema disagree, or if a default value shows up as a literal in the code.
