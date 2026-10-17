"""Scheduled checks of the route's date D (one date per route: no nearby dates).
Runs in a worker thread; every function returns the Telegram messages to send."""
import html
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
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
    """₹/month for active routes (+ an unsaved one)."""
    rows = list(db.q("SELECT * FROM routes WHERE status='active'")) + ([extra] if extra else [])
    return config.monthly_inr(cfg, sum(config.calls_per_day(cfg, anchor_every(cfg, r), slowdown(cfg, r, now))
                                       for r in rows))


def latest_slot(minutes, now_local):
    """Most recent scheduled time today <= now, or None."""
    past = [m for m in minutes if m <= now_local.hour * 60 + now_local.minute]
    return now_local.replace(hour=past[-1] // 60, minute=past[-1] % 60, second=0, microsecond=0) if past else None


def in_active(cfg, now_local):
    a, b = cfg["schedule"]["active_hours"]
    m = now_local.hour * 60 + now_local.minute + now_local.second / 60
    return a * 60 <= m <= b * 60 + 2 * cfg["schedule"]["tick_seconds"] / 60


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
            msgs.append(alerts.for_owner(r, f"✈️ {alerts.route_name(r)}: the {done}, so I've stopped tracking it."))
            continue
        if not in_active(cfg, loc):
            continue
        slot = latest_slot(config.job_slots(cfg, anchor_every(cfg, r), slowdown(cfg, r, now)), loc)
        if not slot or (r["last_anchor_at"] and parse(r["last_anchor_at"]) >= slot):
            continue
        db.update_route(r["id"], last_anchor_at=iso(slot))  # a failed slot is not retried
        try:
            msgs += scan(cfg, r, "anchor", now)
        except Exception as e:  # a bug or bad data in one route must not stop the others
            log.exception("check of route %s failed", r["id"])
            msgs.append(f"⚠️ {alerts.route_name(r)}: check crashed ({html.escape(repr(e))}). See the logs.")
    return msgs


def expired(r, loc):
    if loc.date() > date.fromisoformat(r["anchor_date"]):
        return "date passed"
    if r["flight_no"]:
        o = db.one("SELECT dep_local FROM observations WHERE route_id=? AND depart_date=? AND flight_no=? "
                   "AND price_inr IS NOT NULL ORDER BY id DESC LIMIT 1", (r["id"], r["anchor_date"], r["flight_no"]))
        # dep_local is airport-local time; fine for domestic India (one timezone)
        if o and datetime.fromisoformat(o["dep_local"][:16]) <= loc.replace(tzinfo=None):
            return "flight departed"
    return None


# ---------------------------------------------------------------- one query

def fetch(cfg, r, day, kind, now):
    """One provider query → (observation row, messages). Spend is recorded per attempt, even if
    handling the response then crashes."""
    stops = r["max_stops"] if r["max_stops"] is not None else cfg["search"]["max_stops"]
    obs = {"route_id": r["id"], "depart_date": day, "kind": kind, "observed_at": iso(now), "provider": "ignav"}
    attempts = 0
    try:
        offers, cache_hit, attempts = search(cfg, r["origin"], r["destination"], day, stops)
        best = providers.cheapest(offers, r, stops)
        obs.update(ok=1, cache_hit=int(cache_hit), **(best or {"error": "no flight matches the filters"}))
    except providers.ProviderError as e:
        attempts = e.attempts
        obs.update(ok=0, error=e.message)
        if e.status == 429:
            budget.pause(cfg, now)
    finally:
        msgs = budget.record(cfg, now, kind, attempts, r["owner_id"])
    log.info("ignav %s %s→%s %s ok=%s price=%s err=%s", kind, r["origin"], r["destination"], day,
             obs["ok"], obs.get("price_inr"), obs.get("error"))
    return obs, msgs


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
        return [f"⚠️ {alerts.route_name(r)}: {n} failed checks in a row since "
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


def context(r):
    """What the rules compare a new price of D against, read before that price is saved."""
    lowest, first_seen, samples = db.lowest_seen(r["id"], r["anchor_date"])
    return {"day": r["anchor_date"], "lowest": lowest, "first_seen": first_seen, "samples": samples}


def decide(cfg, r, price, ctx):
    return alerts.decide(cfg, price=price, target=r["target_inr"], target_buffer=r["target_buffer_inr"],
                         states=db.alert_states(r["id"]), **ctx)


def evaluate(cfg, r, obs, ctx, now):
    """Rules for one fresh valid price, re-checked if needed → (spend messages, hits for the message)."""
    fired, clear = decide(cfg, r, obs["price_inr"], ctx)
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
            fired, clear = decide(cfg, r, obs["price_inr"], ctx)
    if not unverified:
        for rule in clear:
            db.clear_alert_state(r["id"], rule, ctx["day"])
    return msgs, [f | {"day": ctx["day"], "price": obs["price_inr"], "obs": obs, "observation_id": obs["id"],
                       "unverified": unverified, "lowest": ctx["lowest"], "first_seen": ctx["first_seen"]}
                  for f in fired]


def book_by_hit(cfg, r, now):
    d = r["anchor_date"]
    cur = db.current_price(r["id"], d, now - timedelta(hours=cfg["deal"]["freshness_h"]))
    lowest, first_seen, _ = db.lowest_seen(r["id"], d)
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


def scan(cfg, r, kind, now):
    """Fetch D, then evaluate every rule together → at most one alert message."""
    msgs, hits = [], []
    if why := budget.blocked(cfg, now):
        msgs += once(f"blocked:{budget.month_key(cfg, now)}:{why}", f"⛔ Ignav checks stopped: {why}.")
    else:
        obs, msgs = fetch(cfg, r, r["anchor_date"], kind, now)
        obs["valid"] = valid(cfg, obs, now)
        ctx = context(r) if obs["valid"] else None
        obs["id"] = db.add_observation(obs)
        if not obs["ok"]:
            msgs += on_failure(cfg, r, obs, now)
        else:
            db.update_route(r["id"], consecutive_failures=0, first_failure_at=None)
            if ctx:
                m, hits = evaluate(cfg, r, obs, ctx, now)
                msgs += m
    if alerts.book_by_due(cfg, local(cfg, now).date(), r["anchor_date"], db.alert_states(r["id"])):
        hits.append(book_by_hit(cfg, r, now))
    if hits:
        hits.sort(key=alerts.priority)
        msgs.append(deliver(cfg, r, hits, now))
    return msgs


def check_now(cfg, rid, now=None):
    """/checknow: one manual check of D, any time of day."""
    return scan(cfg, db.route(rid), "manual", now or datetime.now(UTC))
