"""Scheduled checks: anchor date D, buffer scan, alternative watch, burst.
Runs in a worker thread; every function returns the Telegram messages to send."""
import html
import logging
import uuid
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
PRICED = "valid=1"  # usable prices (see db.py, observations.valid)


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
            msgs += job(cfg, r, now, iso(slot))
    return msgs


def expired(r, loc):
    if loc.date() > date.fromisoformat(r["anchor_date"]):
        return "date passed"
    if r["flight_no"]:
        o = db.one(f"SELECT dep_local FROM observations WHERE route_id=? AND depart_date=? AND flight_no=? "
                   "AND price_inr IS NOT NULL ORDER BY id DESC LIMIT 1", (r["id"], r["anchor_date"], r["flight_no"]))
        # dep_local is airport-local time; fine for domestic India (one timezone)
        if o and datetime.fromisoformat(o["dep_local"][:16]) <= loc.replace(tzinfo=None):
            return "flight departed"
    return None


def job_anchor(cfg, r, now, slot=None):
    return scan(cfg, r, [r["anchor_date"]], "anchor", now, slot)


def job_scan(cfg, r, now, slot=None):
    if r["skip_next_scan"]:  # a burst already refreshed these dates
        db.update_route(r["id"], skip_next_scan=0)
        return []
    msgs = scan(cfg, r, buffer_dates(r["anchor_date"], r["flex_days"], local(cfg, now).date()), "scan", now, slot)
    db.update_route(r["id"], last_alt_at=iso(now))  # the scan just checked the alternative too
    pick_alt(cfg, r["id"], now)
    return msgs


def job_alt(cfg, r, now, slot=None):
    if not r["alt_date"] or r["alt_date"] < local(cfg, now).date().isoformat():
        pick_alt(cfg, r["id"], now)
        return []
    msgs = scan(cfg, r, [r["alt_date"]], "alt", now, slot)
    pick_alt(cfg, r["id"], now)
    return msgs


def pick_alt(cfg, rid, now):
    """Watch the cheapest fresh buffer date if it's cheaper than D or within the near-tie margin."""
    r, d = db.route(rid), cfg["deal"]
    fresh = now - timedelta(hours=d["freshness_h"])
    dp = db.current_price(rid, r["anchor_date"], fresh)
    best = None
    for day in buffer_dates(r["anchor_date"], r["flex_days"], local(cfg, now).date()):
        o = db.current_price(rid, day, fresh)
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


def valid(cfg, obs, now):
    """1 if the price is usable: positive and the flight leaves more than min_lead_h from now."""
    if not (obs["ok"] and isinstance(obs.get("price_inr"), int) and obs["price_inr"] > 0):
        return 0
    # dep_local is airport-local time; fine for domestic India (one timezone)
    dep = datetime.fromisoformat((obs.get("dep_local") or obs["depart_date"])[:16])
    dep = dep.replace(tzinfo=ZoneInfo(cfg["schedule"]["timezone"]))
    return int(dep - now > timedelta(hours=cfg["deal"]["min_lead_h"]))


def context(cfg, r, day, now):
    """What the rules compare a new price of `day` against, read before that price is saved."""
    rid, d = r["id"], cfg["deal"]
    is_anchor = day == r["anchor_date"]
    lowest, first_seen = db.lowest_seen(rid, day)
    dp = None if is_anchor else db.current_price(rid, r["anchor_date"], now - timedelta(hours=d["freshness_h"]))
    return {"day": day, "is_anchor": is_anchor, "lowest": lowest, "first_seen": first_seen,
            "baseline": db.baseline_prices(rid, day, now - timedelta(days=d["baseline_days"])) if is_anchor else [],
            "d_price": dp["price_inr"] if dp else None}


def decide(cfg, r, price, ctx, now):
    return alerts.decide(cfg, now, price=price, target=r["target_inr"], target_buffer=r["target_buffer_inr"],
                         states=db.alert_states(r["id"]), **ctx)


def evaluate(cfg, r, obs, ctx, now):
    """Rules for one fresh valid price, re-checked if needed → (spend messages, hits for the message)."""
    fired, clear = decide(cfg, r, obs["price_inr"], ctx, now)
    msgs, unverified = [], False
    if fired and alerts.needs_recheck(cfg, obs, ctx["lowest"]):
        re = None
        if not budget.blocked(cfg, now):
            re, msgs = fetch(cfg, r, ctx["day"], "recheck", now)
            re |= {"recheck_of": obs["id"], "valid": valid(cfg, re, now)}
            re["id"] = db.add_observation(re)
        tol = cfg["deal"]["recheck_tolerance_pct"]
        if re is None or not re["ok"]:  # could not re-check: send as unverified, rules ignore it
            db.set_valid(obs["id"], None)
            unverified = True
        elif not re["valid"] or re["price_inr"] > obs["price_inr"] * (1 + tol / 100):
            db.set_valid(obs["id"], 0)
            return msgs, []
        else:
            obs = re
            fired, clear = decide(cfg, r, obs["price_inr"], ctx, now)
    if not unverified:
        for rule in clear:
            db.clear_alert_state(r["id"], rule, ctx["day"])
    return msgs, [f | {"day": ctx["day"], "price": obs["price_inr"], "obs": obs, "observation_id": obs["id"],
                       "unverified": unverified, "lowest": ctx["lowest"], "first_seen": ctx["first_seen"]}
                  for f in fired]


def book_by_hit(cfg, r, now):
    d = r["anchor_date"]
    cur = db.current_price(r["id"], d, now - timedelta(hours=cfg["deal"]["freshness_h"]))
    lowest, first_seen = db.lowest_seen(r["id"], d)
    return {"rule": "book_by", "level": None, "saving": None, "day": d, "obs": cur,
            "price": cur["price_inr"] if cur else None, "observation_id": cur["id"] if cur else None,
            "lowest": lowest, "first_seen": first_seen,
            "days_left": (date.fromisoformat(d) - local(cfg, now).date()).days}


def deliver(cfg, r, hits, now):
    """The scan's one message. Its alerts/alert_state rows are written by bot.send once it was sent."""
    batch, lines, states = uuid.uuid4().hex, [], []
    for i, h in enumerate(hits):
        unverified = int(h.get("unverified", False))
        lines.append({"route_id": r["id"], "observation_id": h["observation_id"], "rule": h["rule"],
                      "depart_date": h["day"], "price_inr": h["price"] or h["lowest"] or 0,  # NOT NULL column
                      "saving_inr": h["saving"], "sent_at": iso(now), "level": h["level"],
                      "headline": int(i == 0), "unverified": unverified, "batch": batch})
        if not unverified:
            states.append({"route_id": r["id"], "rule": h["rule"], "depart_date": h["day"], "level": h["level"],
                           "price_inr": h["price"], "saving_inr": h["saving"], "alerted_at": iso(now)})
    m = alerts.message(cfg, r, hits)
    m.record = (lines, states)
    return m


def scan(cfg, r, days, kind, now, slot=None):
    """Fetch each date, then evaluate every rule together → at most one alert message (+ a burst's).
    `slot` (iso) marks scheduled fetches as baseline samples."""
    msgs, hits = [], []
    for day in days:
        if why := budget.blocked(cfg, now):
            msgs += once(f"blocked:{budget.month_key(cfg, now)}:{why}", f"⛔ Ignav checks stopped: {why}.")
            break
        obs, m = fetch(cfg, r, day, kind, now)
        msgs += m
        obs |= {"baseline_slot": slot, "valid": valid(cfg, obs, now)}
        ctx = context(cfg, r, day, now) if obs["valid"] else None
        obs["id"] = db.add_observation(obs)
        if not obs["ok"]:
            msgs += on_failure(cfg, r, obs, now)
            continue
        db.update_route(r["id"], consecutive_failures=0, first_failure_at=None)
        if ctx:
            m, h = evaluate(cfg, r, obs, ctx, now)
            msgs += m
            hits += h
    # only scans that include D, so the anchor and buffer scans of one tick can't both send it
    if r["anchor_date"] in days and alerts.book_by_due(cfg, local(cfg, now).date(), r["anchor_date"],
                                                       db.alert_states(r["id"])):
        hits.append(book_by_hit(cfg, r, now))
    if not hits:
        return msgs
    hits.sort(key=alerts.priority)
    msgs.append(deliver(cfg, r, hits, now))
    head = hits[0]
    if kind != "burst" and head["day"] == r["anchor_date"] and head["rule"] in ("deal", "new_low", "target"):
        msgs += burst(cfg, r, now)
    return msgs


def burst(cfg, r, now):
    """Headline alert on D: search D±burst_flex_days right away, as its own scan. Replaces the next buffer scan."""
    rd = cfg["route_defaults"]
    if r["last_burst_at"] and now - parse(r["last_burst_at"]) < timedelta(hours=rd["burst_cooldown_h"]):
        return []
    # ponytail: burst rows never get a baseline_slot; the baseline is D-only and a burst never fetches D
    msgs = scan(cfg, r, buffer_dates(r["anchor_date"], rd["burst_flex_days"], local(cfg, now).date()), "burst", now)
    db.update_route(r["id"], last_burst_at=iso(now), last_alt_at=iso(now),
                    skip_next_scan=int(rd["burst_flex_days"] >= r["flex_days"]))
    pick_alt(cfg, r["id"], now)
    return msgs


def check_now(cfg, rid, dates, now=None):
    """/checknow: manual checks, any time of day."""
    now = now or datetime.now(UTC)
    msgs = scan(cfg, db.route(rid), dates, "manual", now)
    pick_alt(cfg, rid, now)
    return msgs
