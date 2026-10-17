"""Telegram bot: commands, jobs (tick, heartbeat, summary, backup). Entry point: python bot.py"""
import asyncio
import calendar
import html
import json
import logging
import os
import re
import tempfile
import warnings
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, LinkPreviewOptions, Update
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

🔍 <b>Find &amp; track</b>
/search — guided: city → city → date → track
/track <code>HYD DEL 2026-12-20</code> — track directly
<i>Optional extras for /track:</i>
  <code>target=4500</code> — alert at or below ₹4500
  <code>stops=0</code> — max stops
  <code>airline=6E</code> · <code>flight=6E2134</code>
  <code>time=06-12</code> — departure window (hours)
  <code>bags=1</code> — checked bags
  <code>every=120</code> — check every 120 min
  <code>force</code> — allow over the cost cap

📋 <b>Your routes</b>
/list — your tracked routes (tap one for its buttons)
/history — price history
/calls — every search made, with result
/checknow — check right now
/settarget [<code>4500</code> [buffer] · <code>off</code>]
/pause · /resume · /delete
<i>These ask which route with a button each.</i>
<i>Each route watches one date. For another day, track it too.</i>

⚙️ <b>System</b>
/status — health and spend
/reload — re-read config, show projected cost"""


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
        buf = r["target_buffer_inr"]
        head += f" · 🎯 {inr(r['target_inr'])}" + (f" (+{inr(buf)})" if buf is not None else "")
    if r["status"] != "active":
        head += f" · {r['status']}"
    if r["consecutive_failures"]:
        head += f" · ⚠️ {r['consecutive_failures']} failures"
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
    await say_kb(update, f"Which route for /{cmd}?", kb)
    return None


async def say(update, text):
    await update.effective_message.reply_text(text)


# ---------------------------------------------------------------- commands

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, HELP)


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
    if bad := (set(opts) - {"target", "stops", "airline", "flight", "time", "bags", "every"}) | (flags - {"force"}):
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
        "min_bags": int(opts["bags"]) if "bags" in opts else None,
        "target_inr": int(opts["target"]) if "target" in opts else None,
        "anchor_every_min": every, "status": "active",
    }, "force" in flags


async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg, now = cfg_of(ctx), now_utc()
    try:
        route, force = parse_track(cfg, ctx.args, checker.local(cfg, now).date())
    except ValueError as e:
        return await say(update, f"❌ {html.escape(str(e))}")
    await say(update, save_route(cfg, route | {"owner_id": update.effective_user.id}, force, now))


def save_route(cfg, route, force, now):
    """Insert a parsed route unless it would push projected spend past the hard stop → reply text."""
    route["created_at"] = iso(now)
    before, after = checker.projected(cfg, now), checker.projected(cfg, now, extra=route)
    stop = budget.hard_stop_inr(cfg)
    if after > stop and not force:
        return (f"❌ Not saved. With this route, projected spend is {inr(after)}/month, over the "
                f"{inr(stop)} hard stop. Raise [budget] monthly_cap_inr in config.toml and /reload, "
                f"use every=120, or add 'force' to /track.")
    db.insert("routes", route)
    return (f"✅ Tracking {route_name(route)}. This route ≈ {inr(after - before)}"
            f"/month; all routes {inr(after)} (cap {inr(cfg['budget']['monthly_cap_inr'])}). First check "
            f"within a minute if inside active hours.")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = alerts_view(update.effective_user.id, now_utc())
    await say_kb(update, text, kb)


async def set_status(update, ctx, cmd, status):
    if not (r := await pick(update, ctx, cmd)):
        return
    db.update_route(r["id"], status=status)
    await say(update, f"{route_name(r)} → {status}. {projection_text(cfg_of(ctx), now_utc())}")


async def cmd_delete(update, ctx):
    await set_status(update, ctx, "delete", "deleted")


async def cmd_pause(update, ctx):
    await set_status(update, ctx, "pause", "paused")


async def cmd_resume(update, ctx):
    await set_status(update, ctx, "resume", "active")


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
        f"🎯 {route_line(r, now_utc())}\n\nSend the target price in ₹ (e.g. 8000), optionally with a buffer "
        f"(e.g. 8000 500 alerts at or below ₹8,500; default buffer {inr(cfg_of(ctx)['deal']['target_buffer_inr'])}), "
        f"or \"off\" to remove it.")
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
        return f"🎯 {route_name(r)}: target off"
    if not (1 <= len(nums) <= 2 and all(n.isdigit() for n in nums)):
        return "❓ Send a price like 8000, a price and buffer like 8000 500, or \"off\"."
    target, buf = int(nums[0]), int(nums[1]) if len(nums) > 1 else None
    db.update_route(r["id"], target_inr=target, target_buffer_inr=buf)
    return (f"🎯 {route_name(r)}: target {inr(target)}"
            + (f", alerts at or below {inr(target + buf)}" if buf is not None else " (default buffer)"))


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := await pick(update, ctx, "history")):
        return
    cfg = cfg_of(ctx)
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    rows = db.q(f"SELECT * FROM observations WHERE route_id=? AND depart_date=? AND {checker.PRICED} "
                "ORDER BY observed_at DESC LIMIT ?", (r["id"], r["anchor_date"], cfg["ui"]["history_default_n"]))
    lines = [f"📈 {route_name(r)}"]
    lines += [f"{parse(o['observed_at']).astimezone(tz):%d %b %H:%M} {inr(o['price_inr'])} {o['flight_no']}"
              for o in reversed(rows)]
    await say(update, "\n".join(lines))


def calls_text(cfg, r):
    """Last history_default_n searches for a route: time, kind, date, result, and which ones alerted."""
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    rows = db.q("SELECT o.*, GROUP_CONCAT(a.rule) AS fired FROM observations o "
                "LEFT JOIN alerts a ON a.observation_id=o.id WHERE o.route_id=? "
                "GROUP BY o.id ORDER BY o.observed_at DESC, o.id DESC LIMIT ?",
                (r["id"], cfg["ui"]["history_default_n"]))
    lines = []
    for o in reversed(rows):
        res = (f"{inr(o['price_inr'])} {o['flight_no']}" + (" (cached)" if o["cache_hit"] else "")
               if o["price_inr"] is not None else ("❌ " if not o["ok"] else "") + html.escape(o["error"] or "—"))
        lines.append(f"{parse(o['observed_at']).astimezone(tz):%d %b %H:%M} {o['kind']} {day_label(o['depart_date'])}: "
                     f"{res}" + (f" 🔔 {o['fired']}" if o["fired"] else ""))
    head = f"📜 {route_name(r)} — last {len(rows)} searches"
    while lines and len(head) + sum(len(x) + 1 for x in lines) > MessageLimit.MAX_TEXT_LENGTH:
        lines.pop(0)  # keep the newest that fit in one message
    return "\n".join([head] + lines) if lines else f"📜 {route_name(r)}: no searches yet."


async def cmd_calls(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := await pick(update, ctx, "calls")):
        return
    await say(update, calls_text(cfg_of(ctx), r))


async def cmd_checknow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := await pick(update, ctx, "checknow")):
        return
    cfg, now = cfg_of(ctx), now_utc()
    if why := checknow_quota(cfg, update.effective_user.id, now):
        return await say(update, why)
    await say(update, f"Checking {day_label(r['anchor_date'])}, ≈ ₹{config.per_call_inr(cfg):.2f}…")
    msgs = await asyncio.to_thread(checker.check_now, cfg, r["id"], now)
    for m in msgs:  # alerts go to the route's owner, like a scheduled check
        await (send(ctx, m) if isinstance(m, alerts.Flight) else say(update, m))
    o = db.one("SELECT * FROM observations WHERE route_id=? AND kind='manual' AND observed_at=? "
               "ORDER BY id DESC LIMIT 1", (r["id"], iso(now)))
    await say(update, f"{day_label(r['anchor_date'])} " + (
        inr(o["price_inr"]) + f" {o['flight_no']}" if o and o["price_inr"] is not None
        else html.escape(o["error"] or "") if o else "skipped"))


def checknow_quota(cfg, uid, now):
    """Reserve one manual call for this user today (schedule timezone). Returns an error text if over the limit."""
    limit = cfg["ui"]["checknow_per_user_day"]
    key = f"checknow:{uid}:{checker.local(cfg, now).date()}"
    used = int(db.kv_get(key) or 0)
    if used >= limit:
        return f"⛔ Daily manual-check limit: {used}/{limit} used today. Resets at midnight."
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
    if u and u.id == int(os.environ["ALLOWED_USER_ID"]):
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


MENU = Kb([[Btn("🔍 Flight Search", callback_data="menu:search"), Btn("🔔 My Alerts", callback_data="menu:alerts")],
           [Btn("🎯 Set Target", callback_data="menu:target"), Btn("📊 Status", callback_data="menu:status")],
           [Btn("❓ Help", callback_data="menu:help")]])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✈️ Alright, please choose what we will do 👇", reply_markup=MENU)


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
        await q.message.reply_text(HELP)
    else:
        await q.message.reply_text("✈️ Alright, please choose what we will do 👇", reply_markup=MENU)


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
        s["offers"] = await asyncio.to_thread(run_search, cfg, s["o"], s["d"], s["day"], now)
    except providers.ProviderError as e:
        await say(update, f"⚠️ Search failed: {html.escape(e.message)}")
        return ConversationHandler.END
    text, kb = results_view(cfg, s, update.effective_user.id)
    await say_kb(update, text, kb)
    return ConversationHandler.END


async def search_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, "Cancelled.")
    return ConversationHandler.END


def run_search(cfg, o, d, day, now):
    """One Ignav call for the menu search; counted in the spend ledger like any other."""
    if why := budget.blocked(cfg, now):
        raise providers.ProviderError(why, attempts=0)
    try:
        offers, _, attempts = checker.search(cfg, o, d, day, cfg["search"]["max_stops"], None)
    except providers.ProviderError as e:
        budget.record(cfg, now, "search", e.attempts)
        raise
    budget.record(cfg, now, "search", attempts)
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
            bags = f" · {o['bags']} bag" if o["bags"] else ""
            lines.append(f"\n{i}. ✈️ {o['flight_no']} · {stops}\n    🕐 {o['dep_local'][11:16]}{arr} · <b>{inr(o['price_inr'])}</b>{bags}")
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
    await q.edit_message_reply_markup(results_view(cfg, s, update.effective_user.id)[1])


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
        [Btn("📈 Price history", callback_data=f"p:history:{i}:"), Btn("📜 Searches", callback_data=f"p:calls:{i}:")],
        [Btn("▶️ Resume" if paused else "⏸ Pause", callback_data=f"r:{'active' if paused else 'paused'}:{i}"),
         Btn("❌ Delete", callback_data=f"r:deleted:{i}")],
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
    await q.answer(f"{route_name(r)} → {status}")
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


async def summary(ctx: ContextTypes.DEFAULT_TYPE):
    cfg, now = cfg_of(ctx), now_utc()
    rows = db.q("SELECT * FROM routes WHERE status='active' ORDER BY id")
    await send(ctx, "☀️ Daily summary\n" + "\n".join(route_line(r, now) for r in rows or [])
               + f"\nSpent this month ₹{budget.month_spend(cfg, now):.0f} of {inr(cfg['budget']['monthly_cap_inr'])} · "
               f"free calls left {budget.free_left(cfg)}")


async def backup(ctx: ContextTypes.DEFAULT_TYPE):
    if not cfg_of(ctx)["monitoring"]["admin_notices"]:
        return
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / f"flightbot-{date.today():%Y-%m-%d}.db"
        await asyncio.to_thread(db.backup, dest)
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


async def post_init(app):
    await app.bot.set_my_commands([BotCommand("start", "Menu"), BotCommand("search", "Guided flight search"),
                                   BotCommand("list", "My price alerts"),
                                   BotCommand("settarget", "Set a target price (+ buffer)"),
                                   BotCommand("history", "Price history of a route"),
                                   BotCommand("checknow", "Check a route right now"),
                                   BotCommand("status", "Health and spend"),
                                   BotCommand("help", "All commands")])
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
    if not db.kv_get("baseline_backfilled"):  # one-time after the upgrade to baseline slots
        db.backfill_baseline_slots(["anchor"], cfg["schedule"]["anchor_every_min"])
        db.kv_set("baseline_backfilled", "1")
    build_app(cfg, os.environ["TELEGRAM_TOKEN"]).run_polling(allowed_updates=Update.ALL_TYPES)


# Commands that take a route: typed bare they show pick(); its buttons (p:<cmd>:<id>:…) come back to pick_button.
PICKABLE = {"history": cmd_history, "calls": cmd_calls, "checknow": cmd_checknow,
            "pause": cmd_pause, "resume": cmd_resume, "delete": cmd_delete}


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
