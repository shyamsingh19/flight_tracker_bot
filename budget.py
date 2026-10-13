"""Spend ledger (one row per API attempt), free-call tracking, warnings and the hard stop."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import config
import db


def month_key(cfg, now):
    return now.astimezone(ZoneInfo(cfg["schedule"]["timezone"])).strftime("%Y-%m")


def calls_used():
    return db.one("SELECT COUNT(*) n FROM spend")["n"]


def free_left(cfg):
    return max(0, cfg["budget"]["ignav_free_calls"] - calls_used())


def next_call_inr(cfg):
    return 0.0 if free_left(cfg) else config.per_call_inr(cfg)


def month_spend(cfg, now):
    return db.one("SELECT COALESCE(SUM(est_inr), 0) s FROM spend WHERE month=?", (month_key(cfg, now),))["s"]


def hard_stop_inr(cfg):
    b = cfg["budget"]
    return b["monthly_cap_inr"] * b["hard_stop_pct"] / 100


def blocked(cfg, now):
    """Reason no call may be made right now, or None."""
    until = db.kv_get("ignav_paused_until")
    if until and now < db.parse(until):
        return "paused after an HTTP 429"
    if month_spend(cfg, now) + next_call_inr(cfg) > hard_stop_inr(cfg):
        return f"monthly hard stop ₹{hard_stop_inr(cfg):.0f} reached"
    return None


def pause(cfg, now):
    db.kv_set("ignav_paused_until", db.iso(now + timedelta(minutes=cfg["provider"]["backoff_on_429_min"])))


def record(cfg, now, kind, attempts):
    """Log `attempts` calls; return any budget warnings newly crossed this month."""
    month = month_key(cfg, now)
    for _ in range(attempts):
        db.insert("spend", {"ts": db.iso(now), "month": month, "provider": "ignav", "kind": kind,
                            "est_inr": next_call_inr(cfg)})
    cap = cfg["budget"]["monthly_cap_inr"]
    spent = month_spend(cfg, now)
    msgs = []
    for lvl in cfg["budget"]["warn_at_pct"]:
        key = f"warn:{month}:{lvl}"
        if spent * 100 / cap >= lvl and not db.kv_get(key):
            db.kv_set(key, "1")
            msgs.append(f"💰 Ignav spend this month is ₹{spent:.0f}, {lvl}%+ of the ₹{cap:.0f} cap.")
    return msgs
