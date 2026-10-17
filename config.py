"""Load and validate config.toml. SCHEMA is the only place defaults and ranges live."""
import os
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


def hours(v):
    a, b = map(int, v.split("-"))
    if not 0 <= a < b <= 23:
        raise ValueError("expected 'HH-HH' with 00 <= start < end <= 23")
    return a, b


def hhmm(v):
    h, m = map(int, v.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("expected 'HH:MM'")
    return h * 60 + m


def tz(v):
    ZoneInfo(v)
    return v


# key: (default, kind, min, max). kind is a type, a tuple of allowed values,
# or a parser for a string value (its result is what the code sees).
SCHEMA = {
    "search": {
        "market": ("IN", str),
        "currency": ("INR", str),
        "adults": (1, int, 1, 9),
        "cabin_class": ("economy", ("economy", "premium_economy", "business", "first")),
        "max_stops": (1, int, 0, 2),
        "allow_self_transfer": (False, bool),
    },
    "schedule": {
        "timezone": ("Asia/Kolkata", tz),
        "active_hours": ("08-23", hours),
        "tick_seconds": (60, int, 10, 600),
        "anchor_every_min": (60, int, 1, 1440),
        "far_out_days": (45, int, 1, 365),
        "far_out_slowdown": (3, int, 1, 10),
        "min_interval_min": (15, int, 1, 1440),
    },
    "deal": {
        "baseline_days": (30, int, 1, 365),
        "good_percentile": (25, float, 0, 100),
        "great_percentile": (10, float, 0, 100),
        "min_samples": (20, int, 1, 100000),
        "freshness_h": (26, float, 1, 720),
        "min_lead_h": (3, float, 0, 72),
        "deadline_days": (14, int, 0, 365),
        "suspicious_pct": (30, float, 0, 100),
        "realert_abs_inr": (100, int, 0, 10**6),
        "realert_pct": (3, float, 0, 100),
        "recheck_unverified": (True, bool),
        "recheck_tolerance_pct": (2, float, 0, 100),
        "target_buffer_inr": (300, int, 0, 10**6),
    },
    "provider": {
        "timeout_s": (20, float, 1, 120),
        "retries": (1, int, 0, 5),
        "retry_backoff_s": (2, float, 0, 60),
        "backoff_on_429_min": (60, int, 0, 1440),
    },
    "budget": {
        "monthly_cap_inr": (750, float, 1, 10**6),
        "hard_stop_pct": (90, float, 1, 100),
        "warn_at_pct": ([50, 80, 100], list, 1, 1000),
        "usd_inr_effective": (115, float, 1, 1000),
        "ignav_usd_per_call": (0.002, float, 0, 1),
        "ignav_free_calls": (1000, int, 0, 10**7),
    },
    "monitoring": {
        "fail_notify_after": (3, int, 1, 1000),
        "fail_notify_after_min": (60, int, 1, 10080),
        "fail_notify_cooldown_min": (60, int, 0, 10080),
        "daily_summary_at": ("08:30", hhmm),
        "heartbeat_every_min": (30, int, 1, 1440),
        "backup_at": ("02:00", hhmm),
        "log_level": ("INFO", ("DEBUG", "INFO", "WARNING", "ERROR")),
        "admin_notices": (True, bool),
    },
    "ui": {
        "history_default_n": (20, int, 1, 500),
        "search_results_n": (5, int, 1, 20),
        "checknow_per_user_day": (5, int, 0, 1000),
    },
}


def _coerce(raw, kind, rng):
    if isinstance(kind, tuple):
        if raw not in kind:
            raise ValueError(f"must be one of {', '.join(kind)}")
        return raw
    if kind is list:
        if not isinstance(raw, list) or not raw:
            raise ValueError("must be a non-empty list of integers")
        return [_coerce(x, int, rng) for x in raw]
    if kind in (str, bool):
        if type(raw) is not kind:
            raise ValueError(f"must be a {kind.__name__}")
        return raw
    if kind in (int, float):
        if not (type(raw) is int or (kind is float and type(raw) is float)):
            raise ValueError("must be an integer" if kind is int else "must be a number")
        if not rng[0] <= raw <= rng[1]:
            raise ValueError(f"must be between {rng[0]} and {rng[1]}")
        return raw
    if not isinstance(raw, str):
        raise ValueError("must be a string")
    return kind(raw)


def load(path="config.toml"):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    if unknown := set(data) - set(SCHEMA):
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    cfg = {}
    for sec, keys in SCHEMA.items():
        raw_sec = data.get(sec, {})
        if unknown := set(raw_sec) - set(keys):
            raise ConfigError(f"[{sec}] unknown key(s): {', '.join(sorted(unknown))}")
        cfg[sec] = {}
        for key, (default, kind, *rng) in keys.items():
            raw = raw_sec.get(key, default)
            try:
                cfg[sec][key] = _coerce(raw, kind, rng)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"[{sec}] {key} = {raw!r}: {e}") from None
    _cross_check(cfg)
    return cfg


def _cross_check(cfg):
    d = cfg["deal"]
    if d["great_percentile"] > d["good_percentile"]:
        raise ConfigError("[deal] great_percentile must be <= good_percentile")


def load_env(path=".env"):
    """Secrets: TELEGRAM_TOKEN, IGNAV_API_KEY, ALLOWED_USER_ID, ALERT_USER_IDS, HEARTBEAT_URL. Re-read on /reload."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip("'\"")


# ---- projected cost: everything below is derived from the config ----

def job_slots(cfg, anchor_every_min, slow=1):
    """Minute-of-day slots when your date is searched — used by both the scheduler and the cost formula."""
    a, b = cfg["schedule"]["active_hours"]
    return list(range(a * 60, b * 60 + 1, anchor_every_min * slow))


def calls_per_day(cfg, anchor_every_min, slow=1):
    return len(job_slots(cfg, anchor_every_min, slow))


def per_call_inr(cfg):
    b = cfg["budget"]
    return b["ignav_usd_per_call"] * b["usd_inr_effective"]


def monthly_inr(cfg, calls_a_day):
    return 30 * calls_a_day * per_call_inr(cfg)
