"""SQLite (WAL). A short-lived connection per call keeps the bot thread and the check thread apart.

Tables
  routes        One tracked trip: D (anchor_date), flex_days, target, schedule bookkeeping.
  observations  One row per fetched price for one date. Only `valid` is ever updated.
                  valid          1 = usable, 0 = rejected (bad price or failed re-check),
                                 NULL = re-check could not run (sent as unverified; rules ignore it).
                  baseline_slot  UTC start of the scheduled slot this row fills, else NULL.
                                 At most one row per (route, date, slot); only these rows form
                                 the GOOD/GREAT baseline. Bursts get a slot only when they
                                 replace a scheduled scan; re-checks never do.
                  recheck_of     id of the observation this row re-checked, else NULL.
  alert_state   Re-alert memory, one row per (route, rule, date). Deleting a row resets it.
                  rule: deal (GOOD/GREAT, level says which), new_low, target (level HIT/NEAR),
                        cheaper_date, book_by.
  alerts        Every alert line sent. Lines sent in one message share `batch`.
  spend, kv     Unchanged.

Derived, never stored: lowest seen, current price, baseline (see the query helpers).
All timestamps are UTC ISO strings from iso(), so string comparison = time comparison.
"""
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime

PATH = os.environ.get("DB_PATH", "flightbot.db")
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes(
  id INTEGER PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL, anchor_date TEXT NOT NULL,
  flex_days INTEGER NOT NULL, alt_date TEXT, airline TEXT, flight_no TEXT, max_stops INTEGER,
  time_start INTEGER, time_end INTEGER, min_bags INTEGER, target_inr INTEGER, target_buffer_inr INTEGER,
  anchor_every_min INTEGER, calibration INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, last_anchor_at TEXT, last_scan_at TEXT, last_alt_at TEXT,
  skip_next_scan INTEGER NOT NULL DEFAULT 0, last_burst_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0, first_failure_at TEXT,
  last_fail_notify_at TEXT, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), depart_date TEXT NOT NULL,
  kind TEXT NOT NULL, observed_at TEXT NOT NULL, provider TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
  price_inr INTEGER, price_status TEXT, cache_hit INTEGER, airline TEXT, flight_no TEXT,
  dep_local TEXT, arr_local TEXT, stops INTEGER, bags INTEGER,
  valid INTEGER, baseline_slot TEXT, recheck_of INTEGER REFERENCES observations(id));
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), observation_id INTEGER,
  rule TEXT NOT NULL, depart_date TEXT NOT NULL, price_inr INTEGER NOT NULL, saving_inr INTEGER,
  sent_at TEXT NOT NULL, level TEXT, headline INTEGER NOT NULL DEFAULT 0,
  unverified INTEGER NOT NULL DEFAULT 0, batch TEXT);
CREATE TABLE IF NOT EXISTS alert_state(
  route_id INTEGER NOT NULL REFERENCES routes(id), rule TEXT NOT NULL, depart_date TEXT NOT NULL,
  level TEXT, price_inr INTEGER, saving_inr INTEGER, alerted_at TEXT NOT NULL,
  PRIMARY KEY(route_id, rule, depart_date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS spend(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, month TEXT NOT NULL, provider TEXT NOT NULL,
  kind TEXT NOT NULL, est_inr REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

# Columns added after the first release, so older databases get them. Fresh databases already
# have them from SCHEMA. Indexes on these columns must come after this step (see INDEXES).
ADDED_COLUMNS = {
    "observations": [
        ("arr_local", "TEXT"),  # 2026-10-13
        ("valid", "INTEGER"),
        ("baseline_slot", "TEXT"),
        ("recheck_of", "INTEGER REFERENCES observations(id)"),
    ],
    "routes": [
        ("target_buffer_inr", "INTEGER"),  # NULL = [deal] default
        ("owner_id", "INTEGER"),  # bot.main gives old routes to the admin
    ],
    "alerts": [
        ("level", "TEXT"),
        ("headline", "INTEGER NOT NULL DEFAULT 0"),
        ("unverified", "INTEGER NOT NULL DEFAULT 0"),
        ("batch", "TEXT"),
    ],
}

INDEXES = """
CREATE INDEX IF NOT EXISTS obs_route_date ON observations(route_id, depart_date, observed_at);
CREATE UNIQUE INDEX IF NOT EXISTS obs_slot
  ON observations(route_id, depart_date, baseline_slot) WHERE baseline_slot IS NOT NULL;
CREATE INDEX IF NOT EXISTS obs_lowest
  ON observations(route_id, depart_date, price_inr) WHERE valid = 1;
CREATE INDEX IF NOT EXISTS alerts_route ON alerts(route_id, sent_at);
"""

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _idents(names):
    """Column/table names go into SQL text, so only plain identifiers are allowed."""
    for n in names:
        if not _IDENT.match(n):
            raise ValueError(f"bad identifier: {n!r}")
    return names


# ---------- time ----------

def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def slot_start(dt, minutes):
    """UTC start of the fixed-size slot containing dt, as an iso() string."""
    secs = int(dt.timestamp())
    return iso(datetime.fromtimestamp(secs - secs % (minutes * 60), UTC))


# ---------- connections ----------

def connect():
    c = sqlite3.connect(PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")  # per connection; REFERENCES are not enforced otherwise
    c.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, fewer fsyncs
    return c


@contextmanager
def tx():
    """One atomic multi-statement write. BEGIN IMMEDIATE takes the write lock up front,
    so the bot thread and the check thread cannot interleave inside it."""
    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise


def init():
    with closing(connect()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        c.executescript(INDEXES)
        if c.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            with c:
                # One-time: rows from before `valid` existed are judged by the basic checks.
                c.execute("UPDATE observations SET valid = CASE WHEN ok = 1 AND price_inr > 0"
                          " THEN 1 ELSE 0 END WHERE valid IS NULL")
                c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def backfill_baseline_slots(kinds, slot_minutes):
    """Optional, run once after upgrading: give old scheduled rows a baseline slot so GOOD/GREAT
    is not silent while the baseline refills. `kinds` = the observation kinds your scheduler
    wrote for scheduled scans (not bursts, not re-checks). First row in each slot wins."""
    marks = ", ".join("?" * len(kinds))
    rows = q(f"SELECT id, observed_at FROM observations WHERE baseline_slot IS NULL AND valid = 1"
             f" AND recheck_of IS NULL AND kind IN ({marks}) ORDER BY observed_at, id", tuple(kinds))
    with tx() as c:
        for r in rows:
            c.execute("UPDATE OR IGNORE observations SET baseline_slot=? WHERE id=?",
                      (slot_start(parse(r["observed_at"]), slot_minutes), r["id"]))


# ---------- generic helpers ----------

def q(sql, args=()):
    with closing(connect()) as c, c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with closing(connect()) as c, c:
        return c.execute(sql, args).lastrowid


def _insert(c, table, row):
    cols = ", ".join(_idents([table, *row])[1:])
    return c.execute(f"INSERT INTO {table}({cols}) VALUES({', '.join('?' * len(row))})",
                     tuple(row.values())).lastrowid


def insert(table, row):
    with closing(connect()) as c, c:
        return _insert(c, table, row)


def route(rid):
    return one("SELECT * FROM routes WHERE id=?", (rid,))


def update_route(rid, **fields):
    run(f"UPDATE routes SET {', '.join(f'{k}=?' for k in _idents(list(fields)))} WHERE id=?",
        (*fields.values(), rid))


def kv_get(k):
    r = one("SELECT v FROM kv WHERE k=?", (k,))
    return r["v"] if r else None


def kv_set(k, v):
    run("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def backup(dest):
    with closing(connect()) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)


# ---------- observations ----------

def add_observation(row):
    """Insert one fetched price. Sets `valid` from the basic checks unless the caller set it.
    If row has a baseline_slot that is already filled for that date, the row is still stored
    but without a slot, so each slot keeps exactly one baseline sample."""
    row = dict(row)
    if "valid" not in row:
        price = row.get("price_inr")
        row["valid"] = int(bool(row.get("ok")) and isinstance(price, int) and price > 0)
    with tx() as c:
        if row.get("baseline_slot") and c.execute(
                "SELECT 1 FROM observations WHERE route_id=? AND depart_date=? AND baseline_slot=?",
                (row["route_id"], row["depart_date"], row["baseline_slot"])).fetchone():
            row["baseline_slot"] = None
        return _insert(c, "observations", row)


def set_valid(obs_id, valid):
    """After a re-check: 0 = original rejected, 1 = confirmed, None = re-check failed."""
    run("UPDATE observations SET valid=? WHERE id=?", (valid, obs_id))


def baseline_prices(rid, depart_date, since):
    """D's baseline: one valid scheduled sample per slot since `since`."""
    return [r["price_inr"] for r in q(
        "SELECT price_inr FROM observations WHERE route_id=? AND depart_date=? AND valid=1"
        " AND baseline_slot IS NOT NULL AND observed_at>=?", (rid, depart_date, iso(since)))]


def lowest_seen(rid, depart_date, exclude_id=None):
    """(lowest valid price, first_seen) we recorded for this date, or (None, None).
    Pass the current observation as exclude_id so it is compared against the past only."""
    r = one("SELECT MIN(price_inr) AS price, MIN(observed_at) AS first_seen FROM observations"
            " WHERE route_id=? AND depart_date=? AND valid=1 AND id IS NOT ?",
            (rid, depart_date, exclude_id))
    return (r["price"], r["first_seen"]) if r else (None, None)


def current_price(rid, depart_date, fresh_since):
    """Newest fresh valid observation for this date, or None (date skipped this scan)."""
    return one("SELECT * FROM observations WHERE route_id=? AND depart_date=? AND valid=1"
               " AND observed_at>=? ORDER BY observed_at DESC, id DESC LIMIT 1",
               (rid, depart_date, iso(fresh_since)))


# ---------- alert state and alerts ----------

def alert_states(rid):
    """{(rule, depart_date): row} for one route, for the rules to read."""
    return {(r["rule"], r["depart_date"]): r
            for r in q("SELECT * FROM alert_state WHERE route_id=?", (rid,))}


def clear_alert_state(rid, rule, depart_date):
    """Reset after the deal went away, so the next qualifying price alerts as new."""
    run("DELETE FROM alert_state WHERE route_id=? AND rule=? AND depart_date=?",
        (rid, rule, depart_date))


def record_message(lines, states):
    """Call after the message was sent. Stores every line of one message and updates the
    re-alert state, in one transaction.
      lines:  dicts of alerts columns (all share one `batch`; headline=1 on exactly one)
      states: dicts with route_id, rule, depart_date, level, price_inr, saving_inr, alerted_at
    Unverified lines are stored in alerts but must not be passed in `states`."""
    with tx() as c:
        for line in lines:
            _insert(c, "alerts", line)
        for s in states:
            c.execute(
                "INSERT INTO alert_state(route_id, rule, depart_date, level, price_inr, saving_inr,"
                " alerted_at) VALUES(:route_id, :rule, :depart_date, :level, :price_inr,"
                " :saving_inr, :alerted_at) ON CONFLICT(route_id, rule, depart_date) DO UPDATE SET"
                " level=excluded.level, price_inr=excluded.price_inr,"
                " saving_inr=excluded.saving_inr, alerted_at=excluded.alerted_at", s)
