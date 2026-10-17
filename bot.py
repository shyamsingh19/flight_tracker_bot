"""Telegram bot: commands, jobs (tick, heartbeat, summary, backup). Entry point: python bot.py"""
import asyncio
import calendar
import html
import json
import logging
import os
import re
import warnings
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, LinkPreviewOptions, Update
from telegram.constants import MessageLimit, ParseMode
from telegram.error import TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, Defaults, MessageHandler, TypeHandler, filters)

import alerts
import budget
import checker
import config
import db
import providers
from alerts import day_label, inr, route_name
from db import iso, parse

# The search conversation is per chat and may start from a button; that's intended.
warnings.filterwarnings("ignore", message=".*per_message=False.*", category=PTBUserWarning)
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.toml")
log = logging.getLogger("bot")

HELP = """✈️ <b>Flight price alert</b>
I watch flight prices for your date and message you when it's a good time to book.

🔍 <b>Find &amp; track</b>
/search — find flights and track the price
/track <code>HYD DEL 2026-12-20</code> — track a route directly
<i>You can add:</i>
  <code>target=4500</code> — tell me when it's ₹4,500 or less
  <code>stops=0</code> — direct flights only
  <code>airline=6E</code> or <code>flight=6E2134</code> — one airline or flight
  <code>time=06-12</code> — departing between 06:00 and 12:00

📋 <b>Your routes</b>
/list — your routes; tap one to pause, resume or delete it
/checknow — check a price right now
/settarget — set the price you want to pay
/history — past price checks
<i>Each route is one date. For another day, track that day too.</i>"""
ADMIN_HELP = """

⚙️ <b>Admin</b>
/status — health and spend
/reload — re-read config, show projected cost
/track extras: <code>every=120</code> (minutes between checks), <code>force</code> (save over the cost cap)"""


def is_admin(uid):
    return uid == int(os.environ["ALLOWED_USER_ID"])


def help_text(uid):
    return HELP + (ADMIN_HELP if is_admin(uid) else "")


class JsonLog(logging.Formatter):
    def format(self, r):
        d = {"lvl": r.levelname, "logger": r.name, "msg": r.getMessage()}
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False)


def cfg_of(ctx):
    return ctx.application.bot_data["cfg"]


def now_utc():
    return datetime.now(UTC)


def age(ts, now):
    m = int((now - parse(ts)).total_seconds() // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h" if m < 1440 else f"{m // 1440}d"


def latest_any(rid, day):
    return db.one(f"SELECT * FROM observations WHERE route_id=? AND depart_date=? AND {checker.PRICED} "
                  "ORDER BY observed_at DESC, id DESC LIMIT 1", (rid, day))


def route_line(r, now):
    head = route_name(r) + ("" if r["status"] == "active" else " · " + r["status"])
    parts = [head, price_text(latest_any(r["id"], r["anchor_date"]), now)]
    if r["target_inr"]:
        buf = r["target_buffer_inr"]
        parts.append(f"target {inr(r['target_inr'])}" + (f" +{inr(buf)}" if buf is not None else ""))
    if r["consecutive_failures"]:
        parts.append(f"⚠️ {r['consecutive_failures']} failures")
    return " · ".join(parts)


def price_text(o, now):
    return f"{inr(o['price_inr'])} · {o['flight_no']} · {age(o['observed_at'], now)} ago" if o else "no price yet"


def route_card(r, now):
    """/list view: the route and its latest price."""
    head = f"<b>{route_name(r)}</b>"
    if r["target_inr"]:
        head += f" · 🎯 {inr(r['target_inr'])}"
    if r["status"] != "active":
        head += f" · {r['status']}"
    return f"{head}\n{price_text(latest_any(r['id'], r['anchor_date']), now)}"


def projection_text(cfg, now):
    n = db.one("SELECT COUNT(*) n FROM routes WHERE status='active'")["n"]
    return (f"Projected spend with this config: {inr(checker.projected(cfg, now))}/month for {n} routes "
            f"(cap {inr(cfg['budget']['monthly_cap_inr'])}, hard stop {inr(budget.hard_stop_inr(cfg))}).")


def own_route(update, rid):
    """A live route of the user who sent this update, else None: users only see and change their own routes."""
    r = db.route(rid)
    return r if r and r["status"] != "deleted" and r["owner_id"] == update.effective_user.id else None


def get_route(update, ctx):
    try:
        return own_route(update, int(ctx.args[0]))
    except (IndexError, ValueError, TypeError):
        return None


def live_routes(uid):
    return db.q("SELECT * FROM routes WHERE status IN ('active','paused') AND owner_id=? ORDER BY id", (uid,))


async def pick(update, ctx, cmd):
    """The route given by id (only the picker buttons send one), else None after offering one button per route.
    Any other words the user typed (e.g. 'all', '8000 500') ride along in the button."""
    if r := get_route(update, ctx):
        return r
    rows = live_routes(update.effective_user.id)
    if not rows:
        await say(update, "🔔 No routes yet. Track one from 🔍 Flight Search first.")
        return None
    rest = " ".join(ctx.args or [])[:40]  # callback_data is capped at 64 bytes
    kb = Kb([[Btn(route_name(r) + (" · paused" if r["status"] == "paused" else ""),
                  callback_data=f"p:{cmd}:{r['id']}:{rest}")] for r in rows])
    await say_kb(update, "Which route?", kb)
    return None


async def say(update, text):
    await update.effective_message.reply_text(text)


# ---------------------------------------------------------------- commands

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, help_text(update.effective_user.id))


def parse_track(cfg, args, today):
    if len(args) < 3:
        raise ValueError("usage: /track HYD DEL 2026-12-20 [target=4500] …")
    o, d = args[0].upper(), args[1].upper()
    if not (re.fullmatch(r"[A-Z]{3}", o) and re.fullmatch(r"[A-Z]{3}", d)) or o == d:
        raise ValueError("origin and destination must be two different 3-letter airport codes")
    day = date.fromisoformat(args[2])
    if day < today:
        raise ValueError("that date has passed")
    opts = dict(a.split("=", 1) for a in args[3:] if "=" in a)
    flags = {a.lower() for a in args[3:] if "=" not in a}
    if bad := (set(opts) - {"target", "stops", "airline", "flight", "time", "every"}) | (flags - {"force"}):
        raise ValueError(f"unknown option(s): {', '.join(sorted(bad))}")
    stops = int(opts["stops"]) if "stops" in opts else None
    if stops is not None and not 0 <= stops <= 2:
        raise ValueError("stops must be 0–2")
    t0 = t1 = None
    if "time" in opts:
        t0, t1 = map(int, opts["time"].split("-"))
        if not 0 <= t0 < t1 <= 24:
            raise ValueError("time must look like 06-12")
    every = int(opts["every"]) if "every" in opts else None
    if every is not None and every < cfg["schedule"]["min_interval_min"]:
        raise ValueError(f"every must be ≥ {cfg['schedule']['min_interval_min']} min")
    return {
        "origin": o, "destination": d, "anchor_date": day.isoformat(),
        "airline": opts.get("airline", "").upper() or None,
        "flight_no": opts.get("flight", "").replace(" ", "").upper() or None,
        "max_stops": stops, "time_start": t0, "time_end": t1,
        "target_inr": int(opts["target"]) if "target" in opts else None,
        "anchor_every_min": every, "status": "active",
    }, "force" in flags


async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg, now = cfg_of(ctx), now_utc()
    try:
        route, force = parse_track(cfg, ctx.args, checker.local(cfg, now).date())
    except ValueError as e:
        return await say(update, f"❌ {html.escape(str(e))}")
    uid = update.effective_user.id
    await say(update, save_route(cfg, route | {"owner_id": uid}, force and is_admin(uid), now))


def save_route(cfg, route, force, now):
    """Insert a parsed route unless it would push projected spend past the hard stop → reply text."""
    route["created_at"] = iso(now)
    after = checker.projected(cfg, now, extra=route)
    stop = budget.hard_stop_inr(cfg)
    if after > stop and not force:
        log.warning("route %s not saved for user %s: projected %s/month is over the %s hard stop",
                    route_name(route), route.get("owner_id"), inr(after), inr(stop))
        return "❌ Not saved. Something went wrong, please try again."
    db.insert("routes", route)
    sch, every = cfg["schedule"], checker.anchor_every(cfg, route)
    slow = checker.slowdown(cfg, route, now)
    a, b = sch["active_hours"]
    later = f" (every {every} min once it's {sch['far_out_days']} days away)" if slow > 1 else ""
    return (f"✅ Tracking {route_name(route)}. I'll check the price every {every * slow} min{later}, "
            f"between {a:02d}:00 and {b:02d}:00, and message you when it's a good time to book.")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = alerts_view(update.effective_user.id, now_utc())
    await say_kb(update, text, kb)


TARGET = 3  # conversation state: waiting for the target price


async def target_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settarget [price|off], ☰ Set Target, 🎯 on a route or a picker button: asks for whatever is missing."""
    if q := update.callback_query:
        await q.answer()
        ctx.args = pick_args(q.data)  # t:<id> and p:settarget:<id>:<words>; ☰ menu:target is a bare /settarget
    if not (r := await pick(update, ctx, "settarget")):
        return ConversationHandler.END
    if args := ctx.args[1:]:
        await say(update, set_target(r, " ".join(args)))
        return ConversationHandler.END
    ctx.user_data["target_rid"] = r["id"]
    await update.effective_message.reply_text(
        f"🎯 {route_name(r)}\n\nWhat price do you want to pay? Send it in ₹, e.g. 8000. "
        f"I'll message you when the fare gets there or close. Send \"off\" to remove the target.")
    return TARGET


async def target_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := own_route(update, ctx.user_data.get("target_rid", 0))):
        return ConversationHandler.END
    text = set_target(r, update.message.text)
    await say(update, text)
    return TARGET if text.startswith("❓") else ConversationHandler.END


def set_target(r, text):
    """'8000', '₹8,000 500', '8000 +500' or 'off' → reply text. No buffer given = [deal] default."""
    nums = text.lower().replace(",", "").replace("₹", "").replace("+", " ").split()
    if nums == ["off"]:
        db.update_route(r["id"], target_inr=None, target_buffer_inr=None)
        return f"🎯 {route_name(r)}: target removed."
    if not (1 <= len(nums) <= 2 and all(n.isdigit() for n in nums)):
        return "❓ Send a price like 8000, or \"off\"."
    target, buf = int(nums[0]), int(nums[1]) if len(nums) > 1 else None
    db.update_route(r["id"], target_inr=target, target_buffer_inr=buf)
    return f"🎯 {route_name(r)}: I'll message you when it's {inr(target)} or close to it."


def history_text(cfg, r):
    """/history: the last history_default_n checks of a route: time, result, and which ones alerted."""
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    rows = db.q("SELECT o.*, GROUP_CONCAT(a.rule) AS fired FROM observations o "
                "LEFT JOIN alerts a ON a.observation_id=o.id WHERE o.route_id=? "
                "GROUP BY o.id ORDER BY o.observed_at DESC, o.id DESC LIMIT ?",
                (r["id"], cfg["ui"]["history_default_n"]))
    lines = []
    for o in reversed(rows):
        res = (f"{inr(o['price_inr'])} {o['flight_no']}" if o["price_inr"] is not None
               else "no matching flights" if o["ok"] else "couldn't check")
        lines.append(f"{parse(o['observed_at']).astimezone(tz):%d %b %H:%M}: {res}" + (" 🔔" if o["fired"] else ""))
    head = f"📜 {route_name(r)}: last {len(rows)} checks (🔔 = alert sent)"
    while lines and len(head) + sum(len(x) + 1 for x in lines) > MessageLimit.MAX_TEXT_LENGTH:
        lines.pop(0)  # keep the newest that fit in one message
    return "\n".join([head] + lines) if lines else f"📜 {route_name(r)}: no checks yet."


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := await pick(update, ctx, "history")):
        return
    await say(update, history_text(cfg_of(ctx), r))


async def cmd_checknow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := await pick(update, ctx, "checknow")):
        return
    cfg, now = cfg_of(ctx), now_utc()
    if why := checknow_quota(cfg, update.effective_user.id, now):
        return await say(update, why)
    await say(update, f"Checking {day_label(r['anchor_date'])}…")
    msgs = await asyncio.to_thread(checker.check_now, cfg, r["id"], now)
    for m in msgs:  # alerts to the route's owner, notices (failures, budget) to the admin, like a scheduled check
        await send(ctx, m)
    o = db.one("SELECT * FROM observations WHERE route_id=? AND kind='manual' AND observed_at=? "
               "ORDER BY id DESC LIMIT 1", (r["id"], iso(now)))
    await say(update, f"{day_label(r['anchor_date'])}: " + (
        f"{inr(o['price_inr'])} ({o['flight_no']})" if o and o["price_inr"] is not None
        else "no flights match this route right now." if o and o["ok"]
        else "I couldn't get the price right now. Please try again later."))


def checknow_quota(cfg, uid, now):
    """Reserve one manual call for this user today (schedule timezone). Returns an error text if over the limit."""
    limit = cfg["ui"]["checknow_per_user_day"]
    key = f"checknow:{uid}:{checker.local(cfg, now).date()}"
    used = int(db.kv_get(key) or 0)
    if used >= limit:
        return f"⛔ You've used all {limit} checks for today. Try again tomorrow."
    db.kv_set(key, str(used + 1))
    return None


async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    old = cfg_of(ctx)
    try:
        new = config.load(CONFIG_PATH)
    except config.ConfigError as e:
        return await say(update, f"❌ {html.escape(str(e))}\nKept the previous config.")
    config.load_env()
    changed = [f"{s}.{k}: {old[s][k]!r} → {v!r}" for s in new for k, v in new[s].items() if old[s][k] != v]
    ctx.application.bot_data["cfg"] = new
    schedule_jobs(ctx.application)
    logging.getLogger().setLevel(new["monitoring"]["log_level"])
    await say(update, f"✅ Reloaded. {projection_text(new, now_utc())}\n"
                      + (html.escape("\n".join(changed)) if changed else "No keys changed."))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, status_text(cfg_of(ctx), now_utc()))


def status_text(cfg, now):
    month = budget.month_key(cfg, now)
    last_ok = db.one("SELECT observed_at FROM observations WHERE ok=1 ORDER BY id DESC LIMIT 1")
    last_err = db.one("SELECT observed_at, error FROM observations WHERE ok=0 ORDER BY id DESC LIMIT 1")
    best = db.one("SELECT a.*, r.origin, r.destination FROM alerts a JOIN routes r ON r.id=a.route_id "
                  "WHERE a.rule='new_low' AND a.unverified=0 AND a.sent_at>=? ORDER BY a.saving_inr DESC LIMIT 1",
                  (iso(datetime.fromisoformat(month + "-01").replace(tzinfo=ZoneInfo(cfg["schedule"]["timezone"]))),))
    hb = db.kv_get("last_heartbeat")
    lines = [
        "Ignav: " + (f"last success {age(last_ok['observed_at'], now)} ago" if last_ok else "no successful call yet")
        + (f"; last error {age(last_err['observed_at'], now)} ago: {html.escape(last_err['error'])}" if last_err else ""),
        projection_text(cfg, now),
        f"Spent {month}: ₹{budget.month_spend(cfg, now):.0f} of {inr(cfg['budget']['monthly_cap_inr'])} · "
        f"Ignav free calls left: {budget.free_left(cfg)} ({budget.calls_used()} used)",
        "Best new low this month: " + (f"{best['origin']}→{best['destination']} {day_label(best['depart_date'])} "
                                    f"{inr(best['price_inr'])} (−{inr(best['saving_inr'])})" if best else "none yet"),
    ]
    if (blocked := budget.blocked(cfg, now)):
        lines.append(f"⛔ Checks blocked: {blocked}")
    lines.append("Last heartbeat: " + (f"{age(hb, now)} ago" if hb else "never (HEARTBEAT_URL unset?)"))
    return "\n".join(lines)


ADMIN_ONLY = {"/reload", "/status", "menu:status"}  # server health, spend and config: admin only


def alert_ids():
    return [int(x) for x in os.environ.get("ALERT_USER_IDS", "").replace(",", " ").split()
            if x.lstrip("-").isdigit()]  # a typo is skipped, not fatal


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: the admin gets everything, ALERT_USER_IDS everything but ADMIN_ONLY, others nothing."""
    u = update.effective_user
    if u and is_admin(u.id):
        return
    if not u or u.id not in alert_ids():
        log.warning("ignored update from unauthorized user %s (%s)", u.id if u else None, u.username if u else None)
        raise ApplicationHandlerStop
    q, m = update.callback_query, update.effective_message
    what = q.data if q else (m.text or " ").split()[0].split("@")[0] if m else ""
    if what in ADMIN_ONLY:
        await (q.answer("Admin only") if q else say(update, "⛔ Admin only."))
        raise ApplicationHandlerStop


# ---------------------------------------------------------------- guided flow: menu → search → track

ORIGIN, DEST, DAY = range(3)
# Domestic airports by the names people type. Any 3-letter IATA code also works.
CITIES = {
    "hyderabad": "HYD", "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM", "bombay": "BOM",
    "bengaluru": "BLR", "bangalore": "BLR", "chennai": "MAA", "madras": "MAA", "kolkata": "CCU",
    "calcutta": "CCU", "goa": "GOI", "north goa": "GOX", "mopa": "GOX", "pune": "PNQ", "ahmedabad": "AMD",
    "jaipur": "JAI", "kochi": "COK", "cochin": "COK", "lucknow": "LKO", "guwahati": "GAU",
    "bhubaneswar": "BBI", "patna": "PAT", "srinagar": "SXR", "chandigarh": "IXC", "varanasi": "VNS",
    "visakhapatnam": "VTZ", "vizag": "VTZ", "coimbatore": "CJB", "thiruvananthapuram": "TRV",
    "trivandrum": "TRV", "indore": "IDR", "nagpur": "NAG", "bagdogra": "IXB", "siliguri": "IXB",
    "amritsar": "ATQ", "ranchi": "IXR", "raipur": "RPR", "bhopal": "BHO", "mangaluru": "IXE",
    "mangalore": "IXE", "madurai": "IXM", "tirupati": "TIR", "vijayawada": "VGA", "rajahmundry": "RJA",
    "udaipur": "UDR", "jodhpur": "JDH", "leh": "IXL", "jammu": "IXJ", "dehradun": "DED", "agartala": "IXA",
    "imphal": "IMF", "port blair": "IXZ", "surat": "STV", "vadodara": "BDQ", "aurangabad": "IXU",
    "dibrugarh": "DIB", "kozhikode": "CCJ", "calicut": "CCJ", "kannur": "CNN", "hubli": "HBX",
    "belgaum": "IXG", "belagavi": "IXG", "mysuru": "MYQ", "tiruchirappalli": "TRZ", "trichy": "TRZ",
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def airport(text):
    t = " ".join(text.lower().replace("airport", "").split())
    if t in CITIES:
        return CITIES[t]
    return t.upper() if re.fullmatch(r"[a-z]{3}", t) else None


def parse_day(text, today):
    """'6 november', 'Nov 6', '06.11', '6/11/2026', '2026-11-06', 'tomorrow' → date (next occurrence)."""
    t = " ".join(text.lower().replace(",", " ").split())
    if t in ("today", "tomorrow"):
        return today + timedelta(days=t == "tomorrow")
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    if m := re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?", t):
        d, mo, y = m.groups()
    elif m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)\.?(?: (\d{4}))?", t):
        d, mo, y = m.group(1), MONTHS.get(m.group(2)[:3]), m.group(3)
    elif m := re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", t):
        d, mo, y = m.group(2), MONTHS.get(m.group(1)[:3]), m.group(3)
    else:
        raise ValueError(text)
    if not mo:
        raise ValueError(text)
    if y:
        return date(int(y) + (2000 if len(y) == 2 else 0), int(mo), int(d))
    day = date(today.year, int(mo), int(d))
    return day if day >= today else date(today.year + 1, int(mo), int(d))


def menu(uid):
    return Kb([[Btn("🔍 Flight Search", callback_data="menu:search"), Btn("🔔 My Alerts", callback_data="menu:alerts")],
               [Btn("🎯 Set Target", callback_data="menu:target"), Btn("❓ Help", callback_data="menu:help")]]
              + ([[Btn("📊 Status", callback_data="menu:status")]] if is_admin(uid) else []))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✈️ Alright, please choose what we will do 👇",
                                              reply_markup=menu(update.effective_user.id))


async def menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    what = q.data.split(":")[1]
    if what == "alerts":
        text, kb = alerts_view(update.effective_user.id, now_utc())
        await q.message.reply_text(text, reply_markup=kb)
    elif what == "status":
        await q.message.reply_text(status_text(cfg_of(ctx), now_utc()))
    elif what == "help":
        await q.message.reply_text(help_text(update.effective_user.id))
    else:
        await q.message.reply_text("✈️ Alright, please choose what we will do 👇",
                                   reply_markup=menu(update.effective_user.id))


async def search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text("🏠 What's your departure city? For example, Hyderabad.")
    return ORIGIN


async def search_origin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (code := airport(update.message.text)):
        await say(update, "❓ I don't know that city. Try another name or the 3-letter airport code (e.g. HYD).")
        return ORIGIN
    ctx.user_data["search"] = {"o": code}
    await say(update, f"🛫 From {code}. What's your destination? For example, New Delhi.")
    return DEST


async def search_dest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = airport(update.message.text)
    if not code or code == ctx.user_data["search"]["o"]:
        await say(update, "❓ I don't know that city (or it's the same as the departure). Try again or use the code.")
        return DEST
    ctx.user_data["search"]["d"] = code
    await say(update, f"👍 {ctx.user_data['search']['o']} → {code}. Specify the date of departure "
                      "(e.g. \"6 november\", \"06.11\" or \"2026-11-06\").")
    return DAY


async def search_change_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not {"o", "d"} <= set(ctx.user_data.get("search", {})):
        return await search_start(update, ctx)
    await update.effective_message.reply_text("📅 New date of departure?")
    return DAY


async def search_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg, now = cfg_of(ctx), now_utc()
    today = checker.local(cfg, now).date()
    try:
        day = parse_day(update.message.text, today)
    except ValueError:
        await say(update, "❓ I couldn't read that date. Try \"6 november\" or \"06.11\".")
        return DAY
    if day < today:
        await say(update, "❓ That date has passed. Another one?")
        return DAY
    s = ctx.user_data["search"]
    s.update(day=day.isoformat(), direct=False)
    await say(update, "Ok, give me a few seconds 🔍")
    try:
        s["offers"] = await asyncio.to_thread(run_search, cfg, s["o"], s["d"], s["day"], now,
                                              update.effective_user.id)
    except providers.ProviderError as e:
        log.warning("search %s→%s %s failed: %s", s["o"], s["d"], s["day"], e.message)
        await say(update, "⚠️ I couldn't search right now. Please try again later.")
        return ConversationHandler.END
    text, kb = results_view(cfg, s, update.effective_user.id)
    await say_kb(update, text, kb)
    return ConversationHandler.END


async def search_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, "Cancelled.")
    return ConversationHandler.END


def run_search(cfg, o, d, day, now, uid):
    """One Ignav call for the menu search by user `uid`; counted in the spend ledger like any other."""
    if why := budget.blocked(cfg, now):
        raise providers.ProviderError(why, attempts=0)
    try:
        offers, _, attempts = checker.search(cfg, o, d, day, cfg["search"]["max_stops"])
    except providers.ProviderError as e:
        budget.record(cfg, now, "search", e.attempts, uid)
        raise
    budget.record(cfg, now, "search", attempts, uid)
    return sorted(offers, key=lambda x: x["price_inr"])


def results_view(cfg, s, uid):
    shown = [o for o in s["offers"] if not s["direct"] or o["stops"] == 0][:cfg["ui"]["search_results_n"]]
    head = f"📍 {s['o']} → {s['d']} · {day_label(s['day'])}"
    if not shown:
        lines = [head, "No flights found" + (" (direct only)." if s["direct"] else ".")]
    else:
        lines = [f"{head} · {len(s['offers'])} flights, cheapest {len(shown)}{' direct' if s['direct'] else ''}:"]
        for i, o in enumerate(shown, 1):
            stops = "Direct" if o["stops"] == 0 else f"{o['stops']} stop{'s' * (o['stops'] > 1)}"
            arr = f" → {o['arr_local'][11:16]}" + (" +1" if o["arr_local"][:10] > o["dep_local"][:10] else "") \
                if o.get("arr_local") else ""
            lines.append(f"\n{i}. ✈️ {o['flight_no']} · {stops}\n    🕐 {o['dep_local'][11:16]}{arr} · <b>{inr(o['price_inr'])}</b>")
    lines.append("\n" + alerts.gf_link({"origin": s["o"], "destination": s["d"]}, s["day"]))
    t = tracked(s, uid)
    rows = [[Btn("✅ Tracking (see My Alerts)", callback_data="menu:alerts") if t
             else Btn("🔔 Track prices", callback_data="s:trk")],
            [Btn("🔀 All flights" if s["direct"] else "✈️ Direct only", callback_data="s:direct"),
              Btn("📅 Change date", callback_data="s:date")],
             [Btn("🔍 New search", callback_data="menu:search"), Btn("☰ Menu", callback_data="menu:main")]]
    return "\n".join(lines), Kb(rows)


async def search_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cfg, now = cfg_of(ctx), now_utc()
    s = ctx.user_data.get("search")
    if not s or "offers" not in s:
        await q.answer("This search has expired. Start a new one.", show_alert=True)
        return
    await q.answer()
    if q.data == "s:direct":
        s["direct"] = not s["direct"]
        text, kb = results_view(cfg, s, update.effective_user.id)
        await q.edit_message_text(text, reply_markup=kb)
        return
    uid = update.effective_user.id
    if tracked(s, uid):
        return
    route, _ = parse_track(cfg, [s["o"], s["d"], s["day"]] + (["stops=0"] if s["direct"] else []),
                           checker.local(cfg, now).date())
    await q.message.reply_text(save_route(cfg, route | {"owner_id": uid}, False, now))
    if tracked(s, uid):  # not saved → same buttons, and Telegram rejects an edit that changes nothing
        await q.edit_message_reply_markup(results_view(cfg, s, uid)[1])


def tracked(s, uid):
    """This user's live route watching this search's route + date (any flight), so one search makes one alert."""
    return db.one("SELECT id FROM routes WHERE status IN ('active','paused') AND origin=? AND destination=? "
                  "AND anchor_date=? AND flight_no IS NULL AND max_stops IS ? AND owner_id=?",
                  (s["o"], s["d"], s["day"], 0 if s["direct"] else None, uid))


def alerts_view(uid, now):
    """🔔 My Alerts: every route's card, and one button per route that opens route_view."""
    rows = live_routes(uid)
    if not rows:
        return "🔔 No price alerts yet.", Kb([[Btn("🔍 Flight Search", callback_data="menu:search")]])
    kb = [[Btn(f"⚙️ {route_name(r)}", callback_data=f"v:{r['id']}")] for r in rows]
    return ("🔔 Your price alerts (tap a route below for its options)\n\n"
            + "\n\n".join(route_card(r, now) for r in rows)), Kb(kb)


def route_view(r, now):
    """One route's card with its action buttons; button data carries the id, the labels never show it."""
    i = r["id"]
    paused = r["status"] == "paused"
    return route_card(r, now), Kb([
        [Btn("🔄 Check now", callback_data=f"p:checknow:{i}:"), Btn("🎯 Target", callback_data=f"t:{i}")],
        [Btn("📜 History", callback_data=f"p:history:{i}:"),
         Btn("▶️ Resume" if paused else "⏸ Pause", callback_data=f"r:{'active' if paused else 'paused'}:{i}")],
        [Btn("❌ Delete", callback_data=f"r:deleted:{i}")],
        [Btn("« My Alerts", callback_data="menu:alerts")]])


async def view_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not (r := own_route(update, int(q.data.split(":")[1]))):
        return await q.answer("Route not found")
    await q.answer()
    text, kb = route_view(r, now_utc())
    await q.message.reply_text(text, reply_markup=kb)


async def route_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """⏸ / ▶️ / ❌ in a route view: change status, then redraw it (or the list, after a delete)."""
    q = update.callback_query
    _, status, rid = q.data.split(":")
    if not (r := own_route(update, int(rid))):
        return await q.answer("Route not found")
    db.update_route(r["id"], status=status)
    await q.answer({"active": "Resumed", "paused": "Paused", "deleted": "Deleted"}[status])
    now = now_utc()
    text, kb = route_view(db.route(r["id"]), now) if status != "deleted" else \
        alerts_view(update.effective_user.id, now)
    await q.edit_message_text(text, reply_markup=kb)


def pick_args(data):
    """Callback data → the command args it stands for: 'p:<cmd>:<id>:<words>' and 't:<id>' → [id, *words]."""
    parts = data.split(":", 3)
    return parts[2:3] + parts[3].split() if parts[0] == "p" else parts[1:2] if parts[0] == "t" else []


async def pick_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A route picked from pick() or a route view: run that command as if typed with the route's id."""
    q = update.callback_query
    await q.answer()
    ctx.args = pick_args(q.data)
    await PICKABLE[q.data.split(":")[1]](update, ctx)


async def say_kb(update, text, kb):
    await update.effective_message.reply_text(text, reply_markup=kb)


# ---------------------------------------------------------------- jobs

def recipients(cfg, text):
    """Route messages (alerts.Flight) → that route's owner; anything else → admin, only if admin_notices is on."""
    admin = int(os.environ["ALLOWED_USER_ID"])
    if isinstance(text, alerts.Flight):
        return [text.to or admin]  # each id once, admin first
    return [admin] if cfg["monitoring"]["admin_notices"] else []


async def send(ctx, text):
    for uid in recipients(cfg_of(ctx), text):
        try:
            await ctx.bot.send_message(uid, text)
        except TelegramError as e:  # e.g. that user never pressed Start: don't block the others
            log.warning("send to %s failed: %s", uid, e)
            continue
        if getattr(text, "record", None):  # alert lines + re-alert state, only once actually sent
            db.record_message(*text.record)


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    for m in await asyncio.to_thread(checker.run_due, cfg_of(ctx)):
        await send(ctx, m)


async def heartbeat(ctx: ContextTypes.DEFAULT_TYPE):
    if not (url := os.environ.get("HEARTBEAT_URL")):
        return
    try:
        await asyncio.to_thread(lambda: urlopen(url, timeout=cfg_of(ctx)["provider"]["timeout_s"]).close())
        db.kv_set("last_heartbeat", iso(now_utc()))
    except OSError as e:
        log.warning("heartbeat failed: %s", e)


def report_text(cfg, title, start, end, names):
    """Admin report for [start, end): how many users, what each one used, and the money.
    `names` = {user id: display name}; unknown ids are shown as the number."""
    s, e = iso(start), iso(end)
    admin = int(os.environ["ALLOWED_USER_ID"])
    by = lambda sql: {r[0]: r for r in db.q(sql, (s, e))}  # noqa: E731
    calls = by("SELECT user_id, COUNT(*) n, COALESCE(SUM(est_inr), 0) inr, SUM(kind='manual') manual,"
               " SUM(kind='search') searches FROM spend WHERE ts>=? AND ts<? GROUP BY user_id")
    alerted = by("SELECT r.owner_id, COUNT(*) n FROM alerts a JOIN routes r ON r.id=a.route_id"
                 " WHERE a.headline=1 AND a.sent_at>=? AND a.sent_at<? GROUP BY r.owner_id")
    new = by("SELECT owner_id, COUNT(*) n FROM routes WHERE created_at>=? AND created_at<? GROUP BY owner_id")
    routes = {r[0]: r["n"] for r in db.q("SELECT owner_id, COUNT(*) n FROM routes WHERE status='active' GROUP BY owner_id")}
    total = db.one("SELECT COUNT(*) n, COALESCE(SUM(est_inr), 0) inr FROM spend WHERE ts>=? AND ts<?", (s, e))
    failed = db.one("SELECT COUNT(*) n FROM observations WHERE ok=0 AND observed_at>=? AND observed_at<?", (s, e))["n"]
    users = [admin] + [u for u in alert_ids() if u != admin]
    users += [u for u in calls if u not in users]  # removed users, and calls from before user ids were kept (None)
    used = [u for u in users if u in calls or u in new]
    lines = [f"<b>{title}</b>",
             f"👥 Users: {len(users) - (None in users)} with access, {len(used) - (None in used)} used the bot",
             f"💰 Calls: {total['n']} (₹{total['inr']:.2f}) · this month ₹{budget.month_spend(cfg, end):.0f} of "
             f"{inr(cfg['budget']['monthly_cap_inr'])} · free calls left {budget.free_left(cfg)}"]
    if failed:
        lines.append(f"⚠️ Failed calls: {failed}")
    if why := budget.blocked(cfg, end):
        lines.append(f"⛔ Checks blocked: {why}")
    lines.append("")
    for u in used:
        c = calls.get(u)
        who = "older calls" if u is None else html.escape(names.get(u, str(u))) + (" (admin)" if u == admin else "")
        bits = [f"{c['n']} calls (₹{c['inr']:.2f})" if c else "0 calls"]
        if c and c["manual"]:
            bits.append(f"{c['manual']} check-now")
        if c and c["searches"]:
            bits.append(f"{c['searches']} searches")
        if u is not None:
            bits.append(f"{routes.get(u, 0)} active routes" + (f" (+{new[u]['n']} new)" if u in new else ""))
            bits.append(f"{alerted[u]['n'] if u in alerted else 0} alerts")
        lines.append(f"• {who}: " + " · ".join(bits))
    if not used:
        lines.append("Nobody used the bot.")
    lines += ["", projection_text(cfg, end)]
    return "\n".join(lines)


async def user_names(ctx, uids):
    """Telegram display names for the report; a user the bot can't look up is shown by id."""
    names = {}
    for uid in uids:
        try:
            chat = await ctx.bot.get_chat(uid)
            names[uid] = chat.full_name or chat.username or str(uid)
        except TelegramError:
            pass
    return names


async def summary(ctx: ContextTypes.DEFAULT_TYPE):
    """Admin only: the daily report for today so far, plus the 7-day report on weekly_summary_day."""
    cfg, now = cfg_of(ctx), now_utc()
    if not cfg["monitoring"]["admin_notices"]:
        return
    loc = checker.local(cfg, now)
    today = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    reports = [(f"📊 Daily report · {loc:%a %d %b}", today)]
    if loc.strftime("%a") == cfg["monitoring"]["weekly_summary_day"]:
        week = today - timedelta(days=6)
        reports.append((f"📅 Weekly report · {week:%d %b} – {loc:%d %b}", week))
    names = await user_names(ctx, [int(os.environ["ALLOWED_USER_ID"]), *alert_ids()])
    for title, start in reports:
        await send(ctx, report_text(cfg, title, start, now, names))


def save_backup(keep, today):
    """Copy the live database to backups/<db name>-YYYY-MM-DD.db and delete all but the newest `keep`
    of those. Hand-made copies with other names in backups/ are left alone."""
    live = Path(db.PATH)
    folder = live.parent / "backups"
    folder.mkdir(exist_ok=True)
    dest = folder / f"{live.stem}-{today:%Y-%m-%d}.db"
    db.backup(dest)
    for old in sorted(folder.glob(f"{live.stem}-????-??-??.db"))[:-keep]:
        old.unlink()
    return dest


async def backup(ctx: ContextTypes.DEFAULT_TYPE):
    """Always kept on disk; also sent to the admin (off-machine copy) when admin_notices is on."""
    cfg = cfg_of(ctx)
    dest = await asyncio.to_thread(save_backup, cfg["monitoring"]["backup_keep"], checker.local(cfg, now_utc()).date())
    if cfg["monitoring"]["admin_notices"]:
        with open(dest, "rb") as f:
            await ctx.bot.send_document(int(os.environ["ALLOWED_USER_ID"]), f, caption="Nightly backup")


def schedule_jobs(app):
    cfg = app.bot_data["cfg"]
    s, m = cfg["schedule"], cfg["monitoring"]
    tz = ZoneInfo(s["timezone"])
    jq = app.job_queue
    for j in jq.jobs():
        j.schedule_removal()
    at = lambda mins: time(mins // 60, mins % 60, tzinfo=tz)  # noqa: E731
    jq.run_repeating(tick, interval=s["tick_seconds"], first=5, name="tick",
                     job_kwargs={"max_instances": 1, "coalesce": True})
    jq.run_repeating(heartbeat, interval=m["heartbeat_every_min"] * 60, first=10, name="heartbeat")
    jq.run_daily(summary, at(m["daily_summary_at"]), name="summary")
    jq.run_daily(backup, at(m["backup_at"]), name="backup")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("unhandled error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:  # tell the user instead of going silent
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Please try again.")
        except TelegramError:
            pass


async def post_init(app):
    cmds = [BotCommand("start", "Menu"), BotCommand("search", "Find flights"),
            BotCommand("list", "My price alerts"), BotCommand("settarget", "Set the price you want to pay"),
            BotCommand("checknow", "Check a price now"), BotCommand("history", "Past price checks"),
            BotCommand("help", "How it works")]
    await app.bot.set_my_commands(cmds)
    await app.bot.set_my_commands(cmds + [BotCommand("status", "Health and spend")],
                                  scope=BotCommandScopeChat(int(os.environ["ALLOWED_USER_ID"])))
    if app.bot_data["cfg"]["monitoring"]["admin_notices"]:
        await app.bot.send_message(int(os.environ["ALLOWED_USER_ID"]),
                                   f"🟢 Bot started. {projection_text(app.bot_data['cfg'], now_utc())}")


def main():
    config.load_env()
    cfg = config.load(CONFIG_PATH)
    h = logging.StreamHandler()
    h.setFormatter(JsonLog())
    logging.basicConfig(level=cfg["monitoring"]["log_level"], handlers=[h])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    db.run("UPDATE routes SET owner_id=? WHERE owner_id IS NULL", (int(os.environ["ALLOWED_USER_ID"]),))
    build_app(cfg, os.environ["TELEGRAM_TOKEN"]).run_polling(allowed_updates=Update.ALL_TYPES)


# Commands that take a route: typed bare they show pick(); its buttons (p:<cmd>:<id>:…) come back to pick_button.
# Pause / resume / delete are buttons on each route (route_view), not commands.
PICKABLE = {"history": cmd_history, "checknow": cmd_checknow}


def build_app(cfg, token):
    app = (Application.builder().token(token).post_init(post_init)
           .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
           .build())
    app.bot_data["cfg"] = cfg
    app.add_handler(TypeHandler(Update, gate), group=-1)
    text = filters.TEXT & ~filters.COMMAND
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("search", search_start), CallbackQueryHandler(search_start, "^menu:search$"),
                      CallbackQueryHandler(search_change_date, "^s:date$")],
        states={ORIGIN: [MessageHandler(text, search_origin)], DEST: [MessageHandler(text, search_dest)],
                DAY: [MessageHandler(text, search_day)]},
        fallbacks=[CommandHandler("cancel", search_cancel)], allow_reentry=True))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("settarget", target_start),
                      CallbackQueryHandler(target_start, r"^(t:\d+|p:settarget:.*|menu:target)$")],
        states={TARGET: [MessageHandler(text, target_value)]},
        fallbacks=[CommandHandler("cancel", search_cancel)], allow_reentry=True))
    app.add_handler(CallbackQueryHandler(menu_button, "^menu:"))
    app.add_handler(CallbackQueryHandler(search_button, "^s:"))
    app.add_handler(CallbackQueryHandler(route_button, "^r:"))
    app.add_handler(CallbackQueryHandler(view_button, r"^v:\d+$"))
    app.add_handler(CallbackQueryHandler(pick_button, r"^p:(" + "|".join(PICKABLE) + r"):\d+:"))
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("track", cmd_track), ("list", cmd_list),
                     ("reload", cmd_reload), ("status", cmd_status), *PICKABLE.items()]:
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)
    schedule_jobs(app)
    return app


if __name__ == "__main__":
    main()
