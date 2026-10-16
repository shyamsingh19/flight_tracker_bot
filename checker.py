"""Scheduled checks: anchor date D, buffer scan, alternative watch, burst.
Runs in a worker thread; every function returns the Telegram messages to send."""
import html
import logging
import statistics
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import alerts
import budget
import config
import db
import providers
from db import iso, parse

log = logging.getLogger("checker")
search = providers.ignav_search  # tests replace this
PRICED = "price_inr IS NOT NULL"


def local(cfg, now):
    return now.astimezone(ZoneInfo(cfg["schedule"]["timezone"]))


def anchor_every(cfg, r):
    return max(r["anchor_every_min"] or cfg["schedule"]["anchor_every_min"], cfg["schedule"]["min_interval_min"])


def slowdown(cfg, r, now):
    s = cfg["schedule"]
    far = (date.fromisoformat(r["anchor_date"]) - local(cfg, now).date()).days > s["far_out_days"]
    return s["far_out_slowdown"] if far else 1


def projected(cfg, now, extra=None):
    """(without, with) alternative watch, ₹/month for active routes (+ an unsaved one)."""
    rows = list(db.q("SELECT * FROM routes WHERE status='active'")) + ([extra] if extra else [])
    lo = hi = 0.0
    for r in rows:
        args = (cfg, r["flex_days"], anchor_every(cfg, r), slowdown(cfg, r, now))
        lo += config.calls_per_day(*args, with_alt=False)
        hi += config.calls_per_day(*args, with_alt=True)
    return config.monthly_inr(cfg, lo), config.monthly_inr(cfg, hi)


def latest_slot(minutes, every_days, now_local):
    """Most recent scheduled time <= now, or None."""
    for back in range(every_days + 1):
        day = now_local.date() - timedelta(days=back)
        if day.toordinal() % every_days:
            continue
        for m in reversed(minutes):
            t = datetime.combine(day, time(m // 60, m % 60), now_local.tzinfo)
            if t <= now_local:
                return t
    return None


def in_active(cfg, now_local):
    a, b = cfg["schedule"]["active_hours"]
    m = now_local.hour * 60 + now_local.minute + now_local.second / 60
    return a * 60 <= m <= b * 60 + 2 * cfg["schedule"]["tick_seconds"] / 60


def buffer_dates(anchor, flex, today):
    d = date.fromisoformat(anchor)
    return [(d + timedelta(days=k)).isoformat() for k in range(-flex, flex + 1)
            if k and d + timedelta(days=k) >= today]


def latest(rid, day, since):
    return db.one(f"SELECT * FROM observations WHERE route_id=? AND depart_date=? AND {PRICED} "
                  "AND observed_at>=? ORDER BY observed_at DESC, id DESC LIMIT 1", (rid, day, since))


def once(key, text):
    if db.kv_get(key):
        return []
    db.kv_set(key, "1")
    return [text]


# ---------------------------------------------------------------- the tick

def run_due(cfg, now=None):
    now = now or datetime.now(UTC)
    loc = local(cfg, now)
    msgs = []
    for r in db.q("SELECT * FROM routes WHERE status='active'"):
        if done := expired(r, loc):
            db.update_route(r["id"], status="expired")
            msgs.append(alerts.for_owner(r, f"✅ #{r['id']} {r['origin']}→{r['destination']} {alerts.day_label(r['anchor_date'])} "
                        f"expired ({done}). Tracking stopped."))
            continue
        if not in_active(cfg, loc):
            continue
        jobs = config.job_slots(cfg, anchor_every(cfg, r), slowdown(cfg, r, now))
        for kind, job in (("anchor", job_anchor), ("scan", job_scan), ("alt", job_alt)):
            r = db.route(r["id"])
            slot = latest_slot(*jobs[kind], loc)
            if not slot or (r[f"last_{kind}_at"] and parse(r[f"last_{kind}_at"]) >= slot):
                continue
            db.update_route(r["id"], **{f"last_{kind}_at": iso(slot)})  # a failed slot is not retried
            msgs += job(cfg, r, now)
    return msgs


def expired(r, loc):
    if loc.date() > date.fromisoformat(r["anchor_date"]):
        return "date passed"
    if r["flight_no"]:
        o = db.one(f"SELECT dep_local FROM observations WHERE route_id=? AND depart_date=? AND flight_no=? "
                   f"AND {PRICED} ORDER BY id DESC LIMIT 1", (r["id"], r["anchor_date"], r["flight_no"]))
        # dep_local is airport-local time; fine for domestic India (one timezone)
        if o and datetime.fromisoformat(o["dep_local"][:16]) <= loc.replace(tzinfo=None):
            return "flight departed"
    return None


def job_anchor(cfg, r, now):
    return check(cfg, r, r["anchor_date"], "anchor", now)


def job_scan(cfg, r, now):
    if r["skip_next_scan"]:  # a burst already refreshed these dates
        db.update_route(r["id"], skip_next_scan=0)
        return []
    msgs = []
    for d in buffer_dates(r["anchor_date"], r["flex_days"], local(cfg, now).date()):
        msgs += check(cfg, r, d, "scan", now)
    db.update_route(r["id"], last_alt_at=iso(now))  # the scan just checked the alternative too
    pick_alt(cfg, r["id"], now)
    return msgs


def job_alt(cfg, r, now):
    if not r["alt_date"] or r["alt_date"] < local(cfg, now).date().isoformat():
        pick_alt(cfg, r["id"], now)
        return []
    msgs = check(cfg, r, r["alt_date"], "alt", now)
    pick_alt(cfg, r["id"], now)
    return msgs


def pick_alt(cfg, rid, now):
    """Watch the cheapest fresh buffer date if it's cheaper than D or within the near-tie margin."""
    r, d = db.route(rid), cfg["deal"]
    fresh = iso(now - timedelta(hours=d["freshness_h"]))
    dp = latest(rid, r["anchor_date"], fresh)
    best = None
    for day in buffer_dates(r["anchor_date"], r["flex_days"], local(cfg, now).date()):
        o = latest(rid, day, fresh)
        if o and (best is None or o["price_inr"] < best[1]):
            best = (day, o["price_inr"])
    alt = None
    if best and dp and best[1] - dp["price_inr"] <= max(d["near_tie_inr"], dp["price_inr"] * d["near_tie_pct"] / 100):
        alt = best[0]
    db.update_route(rid, alt_date=alt)


# ---------------------------------------------------------------- one query

def fetch(cfg, r, day, kind, now):
    """One provider query → (observation row, messages). Spend is recorded per attempt."""
    stops = r["max_stops"] if r["max_stops"] is not None else cfg["search"]["max_stops"]
    obs = {"route_id": r["id"], "depart_date": day, "kind": kind, "observed_at": iso(now), "provider": "ignav"}
    try:
        offers, cache_hit, attempts = search(cfg, r["origin"], r["destination"], day, stops, r["min_bags"])
        best = providers.cheapest(offers, r, stops)
        obs.update(ok=1, cache_hit=int(cache_hit), **(best or {"error": "no flight matches the filters"}))
    except providers.ProviderError as e:
        attempts = e.attempts
        obs.update(ok=0, error=e.message)
        if e.status == 429:
            budget.pause(cfg, now)
    log.info("ignav %s %s→%s %s ok=%s price=%s err=%s", kind, r["origin"], r["destination"], day,
             obs["ok"], obs.get("price_inr"), obs.get("error"))
    return obs, budget.record(cfg, now, kind, attempts)


def save(obs):
    return db.insert("observations", obs)


def on_failure(cfg, r, obs, now):
    m = cfg["monitoring"]
    r = db.route(r["id"])
    n = r["consecutive_failures"] + 1
    first = parse(r["first_failure_at"]) if r["first_failure_at"] else now
    db.update_route(r["id"], consecutive_failures=n, first_failure_at=iso(first))
    last = r["last_fail_notify_at"]
    if ((n >= m["fail_notify_after"] or now - first >= timedelta(minutes=m["fail_notify_after_min"]))
            and (not last or now - parse(last) >= timedelta(minutes=m["fail_notify_cooldown_min"]))):
        db.update_route(r["id"], last_fail_notify_at=iso(now))
        return [f"⚠️ #{r['id']} {r['origin']}→{r['destination']}: {n} failed checks in a row since "
                f"{local(cfg, first):%H:%M}. Last error: {html.escape(obs['error'])}. Retrying at the next slot."]
    return []


def context(cfg, r, day, now):
    """Everything decide() and message() need, read before the new observation is saved."""
    d, rid = cfg["deal"], r["id"]
    since = lambda h: iso(now - timedelta(hours=h))  # noqa: E731
    anchor = date.fromisoformat(r["anchor_date"])
    lo, hi = (anchor - timedelta(days=r["flex_days"])).isoformat(), (anchor + timedelta(days=r["flex_days"])).isoformat()
    scalar = lambda sql, *a: db.one(sql, a)[0]  # noqa: E731
    last_grade = db.one("SELECT rule, price_inr FROM alerts WHERE route_id=? AND rule IN ('good','great') "
                        "ORDER BY id DESC LIMIT 1", (rid,))
    last_target = db.one("SELECT price_inr FROM alerts WHERE route_id=? AND rule='target' ORDER BY id DESC LIMIT 1",
                         (rid,))
    last_alt = db.one("SELECT saving_inr, sent_at FROM alerts WHERE route_id=? AND rule='alt' AND depart_date=? "
                      "ORDER BY id DESC LIMIT 1", (rid, day))
    last_flight = db.one("SELECT o.flight_no FROM alerts a JOIN observations o ON o.id=a.observation_id "
                         "WHERE a.route_id=? AND a.depart_date=? ORDER BY a.id DESC LIMIT 1", (rid, day))
    d_latest = latest(rid, r["anchor_date"], since(d["freshness_h"]))
    everything = [x[0] for x in db.q(f"SELECT o.price_inr FROM observations o JOIN routes r ON r.id=o.route_id "
                                      f"WHERE r.origin=? AND r.destination=? AND o.{PRICED}",
                                      (r["origin"], r["destination"]))]
    return {
        "low24": scalar(f"SELECT MIN(price_inr) FROM observations WHERE route_id=? AND depart_date=? AND {PRICED} "
                        "AND observed_at>=?", rid, day, since(d["drop_window_h"])),
        "d_latest": d_latest["price_inr"] if d_latest else None,
        "baseline": [x[0] for x in db.q(f"SELECT price_inr FROM observations WHERE route_id=? AND {PRICED} "
                                        "AND depart_date BETWEEN ? AND ? AND observed_at>=?",
                                        (rid, lo, hi, since(d["baseline_days"] * 24)))],
        "prev_low": scalar(f"SELECT MIN(price_inr) FROM observations WHERE route_id=? AND depart_date=? AND {PRICED}",
                           rid, day),
        "last_grade": tuple(last_grade) if last_grade else None,
        "last_target": last_target[0] if last_target else None,
        "last_alt": (last_alt["saving_inr"], parse(last_alt["sent_at"])) if last_alt else None,
        "last_flight": last_flight[0] if last_flight else None,
        "route_normal": statistics.median(everything) if everything else None,
    }


def check(cfg, r, day, kind, now, quiet=False):
    """Fetch one date, save it, decide alerts (unless quiet) and return messages."""
    if why := budget.blocked(cfg, now):
        return once(f"blocked:{budget.month_key(cfg, now)}:{why}", f"⛔ Ignav checks stopped: {why}.")
    obs, msgs = fetch(cfg, r, day, kind, now)
    if not obs["ok"]:
        save(obs)
        return msgs + on_failure(cfg, r, obs, now)
    db.update_route(r["id"], consecutive_failures=0, first_failure_at=None)
    if obs.get("price_inr") is None or quiet:
        save(obs)
        return msgs
    ctx = context(cfg, r, day, now)
    oid = save(obs)
    is_anchor = day == r["anchor_date"]
    fired = alerts.decide(cfg, now, is_anchor=is_anchor, price=obs["price_inr"], target=r["target_inr"],
                           target_buffer=r["target_buffer_inr"], **ctx)
    if not fired:
        return msgs
    d = cfg["deal"]
    if d["recheck_unverified"] and (obs["price_status"] != "verified" or obs["cache_hit"]):
        re, m2 = fetch(cfg, r, day, "recheck", now)
        msgs += m2
        rid = save(re)
        if not re["ok"] or re.get("price_inr") is None or \
                abs(re["price_inr"] - obs["price_inr"]) > obs["price_inr"] * d["recheck_tolerance_pct"] / 100:
            return msgs
        obs, oid = re, rid
    for a in fired:
        db.insert("alerts", {"route_id": r["id"], "observation_id": oid, "rule": a["rule"], "depart_date": day,
                             "price_inr": obs["price_inr"], "saving_inr": a["saving"], "sent_at": iso(now)})
    nearby = None
    if is_anchor and any(a["rule"] in alerts.RANK or a["rule"] == "target" for a in fired):
        msgs += burst(cfg, r, now)
        nearby = nearby_prices(cfg, db.route(r["id"]), now)
    return msgs + [alerts.message(cfg, r, day, obs, fired, ctx, nearby)]


def burst(cfg, r, now):
    """Good deal on D: search D±burst_flex_days right away. Replaces the next buffer scan."""
    rd = cfg["route_defaults"]
    if r["last_burst_at"] and now - parse(r["last_burst_at"]) < timedelta(hours=rd["burst_cooldown_h"]):
        return []
    msgs = []
    for day in buffer_dates(r["anchor_date"], rd["burst_flex_days"], local(cfg, now).date()):
        msgs += check(cfg, r, day, "burst", now, quiet=True)
    db.update_route(r["id"], last_burst_at=iso(now), last_alt_at=iso(now),
                    skip_next_scan=int(rd["burst_flex_days"] >= r["flex_days"]))
    pick_alt(cfg, r["id"], now)
    return msgs


def nearby_prices(cfg, r, now):
    fresh = iso(now - timedelta(hours=cfg["deal"]["freshness_h"]))
    n = max(r["flex_days"], cfg["route_defaults"]["burst_flex_days"])
    out = [(d, latest(r["id"], d, fresh)) for d in buffer_dates(r["anchor_date"], n, local(cfg, now).date())]
    return [(d, o) for d, o in out if o] or None


def check_now(cfg, rid, dates, now=None):
    """/checknow: manual checks, any time of day."""
    now = now or datetime.now(UTC)
    r = db.route(rid)
    msgs = []
    for d in dates:
        msgs += check(cfg, r, d, "manual", now)
    pick_alt(cfg, rid, now)
    return msgs
