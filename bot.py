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
from telegram.constants import ParseMode
from telegram.warnings import PTBUserWarning
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, Defaults, MessageHandler, TypeHandler, filters)

import alerts
import budget
import checker
import config
import db
import providers
from alerts import day_label, inr
from db import iso, parse

# The search conversation is per chat and may start from a button; that's intended.
warnings.filterwarnings("ignore", message=".*per_message=False.*", category=PTBUserWarning)
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.toml")
log = logging.getLogger("bot")

HELP = """✈️ Flight price alert
/search — guided search: city → city → date → track that route + date
/track HYD DEL 2026-12-20 [flex=5] [target=4500] [stops=0] [airline=6E] [flight=6E2134] [time=06-12] [bags=1] [every=120] [force]
/list · /history id · /status
/checknow id [all] · /settarget [id] [4500|off]
/pause id · /resume id · /delete id
/reload — re-read config.toml and show the projected cost"""


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
    head = (f"#{r['id']} {r['origin']}→{r['destination']} {day_label(r['anchor_date'])} ±{r['flex_days']}"
            f"{'' if r['status'] == 'active' else ' · ' + r['status']}")
    o = latest_any(r["id"], r["anchor_date"])
    parts = [head, f"D {inr(o['price_inr'])} ({age(o['observed_at'], now)} ago, {o['flight_no']})" if o else "D: no price yet"]
    if r["alt_date"] and (a := latest_any(r["id"], r["alt_date"])):
        parts.append(f"alt {day_label(r['alt_date'])} {inr(a['price_inr'])} ({age(a['observed_at'], now)} ago)")
    if r["target_inr"]:
        parts.append(f"target {inr(r['target_inr'])}")
    if r["consecutive_failures"]:
        parts.append(f"⚠️ {r['consecutive_failures']} failures")
    return " · ".join(parts)


def projection_text(cfg, now):
    lo, hi = checker.projected(cfg, now)
    n = db.one("SELECT COUNT(*) n FROM routes WHERE status='active'")["n"]
    return (f"Projected spend with this config: {inr(lo)}–{inr(hi)}/month for {n} routes "
            f"(cap {inr(cfg['budget']['monthly_cap_inr'])}, hard stop {inr(budget.hard_stop_inr(cfg))}).")


def get_route(ctx):
    try:
        r = db.route(int(ctx.args[0]))
    except (IndexError, ValueError):
        return None
    return r if r and r["status"] != "deleted" else None


async def say(update, text):
    await update.effective_message.reply_text(text)


# ---------------------------------------------------------------- commands

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await say(update, html.escape(HELP))


def parse_track(cfg, args, today):
    rd = cfg["route_defaults"]
    if len(args) < 3:
        raise ValueError("usage: /track HYD DEL 2026-12-20 [flex=5] [target=4500] …")
    o, d = args[0].upper(), args[1].upper()
    if not (re.fullmatch(r"[A-Z]{3}", o) and re.fullmatch(r"[A-Z]{3}", d)) or o == d:
        raise ValueError("origin and destination must be two different 3-letter airport codes")
    day = date.fromisoformat(args[2])
    if day < today:
        raise ValueError("that date has passed")
    opts = dict(a.split("=", 1) for a in args[3:] if "=" in a)
    flags = {a.lower() for a in args[3:] if "=" not in a}
    if bad := (set(opts) - {"flex", "target", "stops", "airline", "flight", "time", "bags", "every"}) | (flags - {"force"}):
        raise ValueError(f"unknown option(s): {', '.join(sorted(bad))}")
    flex = int(opts.get("flex", rd["flex_days"]))
    if not 0 <= flex <= rd["max_flex_days"]:
        raise ValueError(f"flex must be 0–{rd['max_flex_days']}")
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
        "origin": o, "destination": d, "anchor_date": day.isoformat(), "flex_days": flex,
        "airline": opts.get("airline", "").upper() or None,
        "flight_no": opts.get("flight", "").replace(" ", "").upper() or None,
        "max_stops": stops, "time_start": t0, "time_end": t1,
        "min_bags": int(opts["bags"]) if "bags" in opts else None,
        "target_inr": int(opts["target"]) if "target" in opts else None,
        "anchor_every_min": every, "calibration": 0, "status": "active",
    }, "force" in flags


async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg, now = cfg_of(ctx), now_utc()
    try:
        route, force = parse_track(cfg, ctx.args, checker.local(cfg, now).date())
    except ValueError as e:
        return await say(update, f"❌ {html.escape(str(e))}")
    await say(update, save_route(cfg, route, force, now))


def save_route(cfg, route, force, now):
    """Insert a parsed route unless it would push projected spend past the hard stop → reply text."""
    route["created_at"] = iso(now)
    route["calibration"] = int(cfg["calibration"]["enabled"] and route["anchor_every_min"] is None
                               and not db.one("SELECT 1 FROM routes WHERE calibration=1"))
    lo0, hi0 = checker.projected(cfg, now)
    lo, hi = checker.projected(cfg, now, extra=route)
    stop = budget.hard_stop_inr(cfg)
    if hi > stop and not force:
        return (f"❌ Not saved. With this route, projected spend is {inr(lo)}–{inr(hi)}/month, over the "
                f"{inr(stop)} hard stop. Raise [budget] monthly_cap_inr in config.toml and /reload, "
                f"use a smaller flex= or every=120, or add 'force' to /track.")
    rid = db.insert("routes", route)
    note = (f"\n🧪 Calibration route: D every {cfg['calibration']['anchor_every_min']} min for "
            f"{cfg['calibration']['days']} days." if route["calibration"] else "")
    what = " ".join(x for x in [route["flight_no"], "direct only" if route["max_stops"] == 0 else ""] if x)
    return (f"✅ Tracking #{rid} {route['origin']}→{route['destination']} {day_label(route['anchor_date'])} "
            f"±{route['flex_days']} days{' · ' + what if what else ''}. This route ≈ {inr(lo - lo0)}–{inr(hi - hi0)}"
            f"/month; all routes {inr(lo)}–{inr(hi)} (cap {inr(cfg['budget']['monthly_cap_inr'])}). First check "
            f"within a minute if inside active hours.{note}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = alerts_view(now_utc())
    await say_kb(update, text, kb)


async def set_status(update, ctx, status):
    if not (r := get_route(ctx)):
        return await say(update, "❌ Unknown route id.")
    db.update_route(r["id"], status=status)
    await say(update, f"#{r['id']} → {status}. {projection_text(cfg_of(ctx), now_utc())}")


async def cmd_delete(update, ctx):
    await set_status(update, ctx, "deleted")


async def cmd_pause(update, ctx):
    await set_status(update, ctx, "paused")


async def cmd_resume(update, ctx):
    await set_status(update, ctx, "active")


TARGET = 3  # conversation state: waiting for the target price


async def target_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/settarget [id] [price|off], or the 🎯 button: asks for whatever is missing."""
    if q := update.callback_query:
        await q.answer()
        r = db.route(int(q.data.split(":")[1]))
    else:
        args = ctx.args or []
        live = {x["id"]: x for x in db.q("SELECT * FROM routes WHERE status IN ('active','paused') ORDER BY id")}
        if args and args[0].isdigit() and int(args[0]) in live:
            r, args = live[int(args[0])], args[1:]
        elif len(live) == 1 and len(args) < 2:  # "/settarget 8000" with a single route
            r = next(iter(live.values()))
        else:
            await say(update, "Which route? Send /settarget id, e.g. /settarget 3 (ids are in /list)."
                      if live else "🔔 No routes yet. Track one from 🔍 Flight Search first.")
            return ConversationHandler.END
        if args:
            await say(update, set_target(r, args[0]))
            return ConversationHandler.END
    ctx.user_data["target_rid"] = r["id"]
    await update.effective_message.reply_text(
        f"🎯 {route_line(r, now_utc())}\n\nSend the target price in ₹ (e.g. 8000), or \"off\" to remove it. "
        f"You'll be alerted at or below target + {inr(cfg_of(ctx)['deal']['target_buffer_inr'])}.")
    return TARGET


async def target_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = db.route(ctx.user_data.get("target_rid", 0))
    if not r:
        return ConversationHandler.END
    text = set_target(r, update.message.text.strip().replace(",", "").lstrip("₹"))
    await say(update, text)
    return TARGET if text.startswith("❓") else ConversationHandler.END


def set_target(r, arg):
    arg = arg.lower()
    if not (arg == "off" or arg.isdigit()):
        return "❓ Send a price like 8000, or \"off\"."
    target = None if arg == "off" else int(arg)
    db.update_route(r["id"], target_inr=target)
    return f"🎯 #{r['id']} target → {inr(target) if target else 'off'}"


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := get_route(ctx)):
        return await say(update, "Usage: /history id")
    cfg, now = cfg_of(ctx), now_utc()
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    rows = db.q(f"SELECT * FROM observations WHERE route_id=? AND depart_date=? AND {checker.PRICED} "
                "ORDER BY observed_at DESC LIMIT ?", (r["id"], r["anchor_date"], cfg["ui"]["history_default_n"]))
    lines = [f"#{r['id']} {r['origin']}→{r['destination']} D = {day_label(r['anchor_date'])}"]
    lines += [f"{parse(o['observed_at']).astimezone(tz):%d %b %H:%M} {inr(o['price_inr'])} {o['flight_no']}"
              for o in reversed(rows)]
    lines.append("Buffer dates (latest):")
    for d in checker.buffer_dates(r["anchor_date"], r["flex_days"], checker.local(cfg, now).date()):
        o = latest_any(r["id"], d)
        lines.append(f"{day_label(d)} " + (f"{inr(o['price_inr'])} ({age(o['observed_at'], now)} ago)" if o else "—"))
    await say(update, "\n".join(lines))


async def cmd_checknow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not (r := get_route(ctx)):
        return await say(update, "Usage: /checknow id [all]")
    cfg, now = cfg_of(ctx), now_utc()
    words = {a.lower() for a in ctx.args[1:]}
    dates = [r["anchor_date"]]
    if "all" in words:
        dates += checker.buffer_dates(r["anchor_date"], r["flex_days"], checker.local(cfg, now).date())
    cost = len(dates) * config.per_call_inr(cfg)
    if cost > cfg["ui"]["checknow_confirm_above_inr"] and "yes" not in words:
        return await say(update, f"This runs {len(dates)} calls ≈ ₹{cost:.1f} (free calls left: {budget.free_left(cfg)}). "
                                 f"Send /checknow {r['id']} {' '.join(ctx.args[1:])} yes to run it.")
    await say(update, f"Checking {len(dates)} date(s), ≈ ₹{cost:.2f}…")
    msgs = await asyncio.to_thread(checker.check_now, cfg, r["id"], dates, now)
    for m in msgs:
        await say(update, m)
    lines = []
    for d in dates:
        o = db.one("SELECT * FROM observations WHERE route_id=? AND depart_date=? AND kind='manual' AND observed_at=? "
                   "ORDER BY id DESC LIMIT 1", (r["id"], d, iso(now)))
        lines.append(f"{day_label(d)} " + (inr(o["price_inr"]) + f" {o['flight_no']}" if o and o["price_inr"] is not None
                                           else html.escape(o["error"] or "") if o else "skipped"))
    await say(update, "\n".join(lines))


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
                  "WHERE a.rule='drop' AND a.sent_at>=? ORDER BY a.saving_inr DESC LIMIT 1",
                  (iso(datetime.fromisoformat(month + "-01").replace(tzinfo=ZoneInfo(cfg["schedule"]["timezone"]))),))
    hb = db.kv_get("last_heartbeat")
    lines = [
        "Ignav: " + (f"last success {age(last_ok['observed_at'], now)} ago" if last_ok else "no successful call yet")
        + (f"; last error {age(last_err['observed_at'], now)} ago: {html.escape(last_err['error'])}" if last_err else ""),
        projection_text(cfg, now),
        f"Spent {month}: ₹{budget.month_spend(cfg, now):.0f} of {inr(cfg['budget']['monthly_cap_inr'])} · "
        f"Ignav free calls left: {budget.free_left(cfg)} ({budget.calls_used()} used)",
        "Best drop this month: " + (f"{best['origin']}→{best['destination']} {day_label(best['depart_date'])} "
                                    f"{inr(best['price_inr'])} (−{inr(best['saving_inr'])})" if best else "none yet"),
    ]
    if (blocked := budget.blocked(cfg, now)):
        lines.append(f"⛔ Checks blocked: {blocked}")
    for r in db.q("SELECT * FROM routes WHERE calibration=1"):
        if checker.calibrating(cfg, r, now):
            day = (now - parse(r["created_at"])).days + 1
            lines.append(f"🧪 Calibration on #{r['id']}: day {day} of {cfg['calibration']['days']}")
        else:
            lines.append(checker.calibration_report(cfg, checker.calibration_series(cfg, r), r["flex_days"])[1])
    lines.append("Last heartbeat: " + (f"{age(hb, now)} ago" if hb else "never (HEARTBEAT_URL unset?)"))
    return "\n".join(lines)


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: anyone but ALLOWED_USER_ID gets no reply."""
    u = update.effective_user
    if not u or u.id != int(os.environ["ALLOWED_USER_ID"]):
        log.warning("ignored update from unauthorized user %s (%s)", u.id if u else None, u.username if u else None)
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
           [Btn("📊 Status", callback_data="menu:status"), Btn("❓ Help", callback_data="menu:help")]])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✈️ Alright, please choose what we will do 👇", reply_markup=MENU)


async def menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    what = q.data.split(":")[1]
    if what == "alerts":
        text, kb = alerts_view(now_utc())
        await q.message.reply_text(text, reply_markup=kb)
    elif what == "status":
        await q.message.reply_text(status_text(cfg_of(ctx), now_utc()))
    elif what == "help":
        await q.message.reply_text(html.escape(HELP))
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
    text, kb = results_view(cfg, s)
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


def results_view(cfg, s):
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
    t = tracked(s)
    rows = [[Btn(f"✅ Tracking #{t['id']}", callback_data="menu:alerts") if t
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
        text, kb = results_view(cfg, s)
        await q.edit_message_text(text, reply_markup=kb)
        return
    if tracked(s):
        return
    route, _ = parse_track(cfg, [s["o"], s["d"], s["day"]] + (["stops=0"] if s["direct"] else []),
                           checker.local(cfg, now).date())
    await q.message.reply_text(save_route(cfg, route, False, now))
    await q.edit_message_reply_markup(results_view(cfg, s)[1])


def tracked(s):
    """The live route watching this search's route + date (any flight), so one search makes one alert."""
    return db.one("SELECT id FROM routes WHERE status IN ('active','paused') AND origin=? AND destination=? "
                  "AND anchor_date=? AND flight_no IS NULL AND max_stops IS ?",
                  (s["o"], s["d"], s["day"], 0 if s["direct"] else None))


def alerts_view(now):
    rows = db.q("SELECT * FROM routes WHERE status IN ('active','paused') ORDER BY id")
    if not rows:
        return "🔔 No price alerts yet.", Kb([[Btn("🔍 Flight Search", callback_data="menu:search")]])
    kb = [[Btn(f"❌ Delete #{r['id']}", callback_data=f"r:deleted:{r['id']}"),
           Btn(f"🎯 Target #{r['id']}", callback_data=f"t:{r['id']}"),
           Btn(f"⏸ Pause #{r['id']}" if r["status"] == "active" else f"▶️ Resume #{r['id']}",
               callback_data=f"r:{'paused' if r['status'] == 'active' else 'active'}:{r['id']}")] for r in rows]
    return "🔔 Your price alerts:\n\n" + "\n\n".join(route_line(r, now) for r in rows), Kb(kb)


async def route_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, status, rid = q.data.split(":")
    db.update_route(int(rid), status=status)
    await q.answer(f"#{rid} → {status}")
    text, kb = alerts_view(now_utc())
    await q.edit_message_text(text, reply_markup=kb)


async def say_kb(update, text, kb):
    await update.effective_message.reply_text(text, reply_markup=kb)


# ---------------------------------------------------------------- jobs

async def send(ctx, text):
    await ctx.bot.send_message(int(os.environ["ALLOWED_USER_ID"]), text)


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
                                   BotCommand("list", "My price alerts"), BotCommand("status", "Health and spend"),
                                   BotCommand("help", "All commands")])
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
    build_app(cfg, os.environ["TELEGRAM_TOKEN"]).run_polling(allowed_updates=Update.ALL_TYPES)


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
        entry_points=[CommandHandler("settarget", target_start), CallbackQueryHandler(target_start, r"^t:\d+$")],
        states={TARGET: [MessageHandler(text, target_value)]},
        fallbacks=[CommandHandler("cancel", search_cancel)], allow_reentry=True))
    app.add_handler(CallbackQueryHandler(menu_button, "^menu:"))
    app.add_handler(CallbackQueryHandler(search_button, "^s:"))
    app.add_handler(CallbackQueryHandler(route_button, "^r:"))
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("track", cmd_track), ("list", cmd_list),
                     ("delete", cmd_delete), ("pause", cmd_pause), ("resume", cmd_resume),
                     ("history", cmd_history), ("checknow", cmd_checknow),
                     ("reload", cmd_reload), ("status", cmd_status)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_error_handler(on_error)
    schedule_jobs(app)
    return app


if __name__ == "__main__":
    main()
