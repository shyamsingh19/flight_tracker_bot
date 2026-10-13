"""Telegram bot: commands, jobs (tick, heartbeat, summary, backup). Entry point: python bot.py"""
import asyncio
import html
import json
import logging
import os
import re
import tempfile
from datetime import UTC, date, datetime, time
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults, MessageHandler, filters

import budget
import checker
import config
import db
from alerts import day_label, inr
from db import iso, parse

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.toml")
log = logging.getLogger("bot")

HELP = """✈️ Flight price alert
/track HYD DEL 2026-12-20 [flex=5] [target=4500] [stops=0] [airline=6E] [flight=6E2134] [time=06-12] [bags=1] [every=120] [force]
/list · /history id · /status
/checknow id [all] · /settarget id 4500|off
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

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    route["created_at"] = iso(now)
    route["calibration"] = int(cfg["calibration"]["enabled"] and route["anchor_every_min"] is None
                               and not db.one("SELECT 1 FROM routes WHERE calibration=1"))
    lo0, hi0 = checker.projected(cfg, now)
    lo, hi = checker.projected(cfg, now, extra=route)
    stop = budget.hard_stop_inr(cfg)
    if hi > stop and not force:
        return await say(update, f"❌ Not saved. With this route, projected spend is {inr(lo)}–{inr(hi)}/month, over the "
                                 f"{inr(stop)} hard stop. Raise [budget] monthly_cap_inr in config.toml and /reload, "
                                 f"use a smaller flex= or every=120, or add 'force'.")
    rid = db.insert("routes", route)
    note = (f"\n🧪 Calibration route: D every {cfg['calibration']['anchor_every_min']} min for "
            f"{cfg['calibration']['days']} days." if route["calibration"] else "")
    await say(update, f"✅ Tracking #{rid} {route['origin']}→{route['destination']} {day_label(route['anchor_date'])} "
                      f"±{route['flex_days']}. This route ≈ {inr(lo - lo0)}–{inr(hi - hi0)}/month; all routes "
                      f"{inr(lo)}–{inr(hi)} (cap {inr(cfg['budget']['monthly_cap_inr'])}). First check within a "
                      f"minute if inside active hours.{note}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = now_utc()
    rows = db.q("SELECT * FROM routes WHERE status IN ('active','paused') ORDER BY id")
    await say(update, "\n".join(route_line(r, now) for r in rows) or "No routes. Use /track.")


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


async def cmd_settarget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = get_route(ctx)
    arg = ctx.args[1].lower() if r and len(ctx.args) > 1 else ""
    if not (arg == "off" or arg.isdigit()):
        return await say(update, "Usage: /settarget id 4500|off")
    target = None if arg == "off" else int(arg)
    db.update_route(r["id"], target_inr=target)
    await say(update, f"#{r['id']} target → {inr(target) if target else 'off'}")


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


async def unauthorized(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    log.warning("ignored message from unauthorized user %s (%s)", u.id if u else None, u.username if u else None)


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
    auth = filters.User(user_id=int(os.environ["ALLOWED_USER_ID"]))
    app = (Application.builder().token(os.environ["TELEGRAM_TOKEN"]).post_init(post_init)
           .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
           .build())
    app.bot_data["cfg"] = cfg
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("track", cmd_track), ("list", cmd_list),
                     ("delete", cmd_delete), ("pause", cmd_pause), ("resume", cmd_resume),
                     ("settarget", cmd_settarget), ("history", cmd_history), ("checknow", cmd_checknow),
                     ("reload", cmd_reload), ("status", cmd_status)]:
        app.add_handler(CommandHandler(name, fn, filters=auth))
    app.add_handler(MessageHandler(~auth, unauthorized))
    app.add_error_handler(on_error)
    schedule_jobs(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
