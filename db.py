"""SQLite (WAL). A short-lived connection per call keeps the bot thread and the check thread apart."""
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

PATH = os.environ.get("DB_PATH", "flightbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes(
  id INTEGER PRIMARY KEY, origin TEXT NOT NULL, destination TEXT NOT NULL, anchor_date TEXT NOT NULL,
  flex_days INTEGER NOT NULL, alt_date TEXT, airline TEXT, flight_no TEXT, max_stops INTEGER,
  time_start INTEGER, time_end INTEGER, min_bags INTEGER, target_inr INTEGER, target_buffer_inr INTEGER, anchor_every_min INTEGER,
  calibration INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
  last_anchor_at TEXT, last_scan_at TEXT, last_alt_at TEXT, skip_next_scan INTEGER NOT NULL DEFAULT 0,
  last_burst_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, first_failure_at TEXT,
  last_fail_notify_at TEXT);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), depart_date TEXT NOT NULL,
  kind TEXT NOT NULL, observed_at TEXT NOT NULL, provider TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,
  price_inr INTEGER, price_status TEXT, cache_hit INTEGER, airline TEXT, flight_no TEXT, dep_local TEXT, arr_local TEXT,
  stops INTEGER, bags INTEGER);
CREATE INDEX IF NOT EXISTS obs_route_date ON observations(route_id, depart_date, observed_at);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY, route_id INTEGER NOT NULL REFERENCES routes(id), observation_id INTEGER,
  rule TEXT NOT NULL, depart_date TEXT NOT NULL, price_inr INTEGER NOT NULL, saving_inr INTEGER,
  sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, month TEXT NOT NULL, provider TEXT NOT NULL,
  kind TEXT NOT NULL, est_inr REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(s):
    return datetime.fromisoformat(s)


def connect():
    c = sqlite3.connect(PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with closing(connect()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        if "arr_local" not in {r[1] for r in c.execute("PRAGMA table_info(observations)")}:
            c.execute("ALTER TABLE observations ADD COLUMN arr_local TEXT")  # databases from before 2026-10-13
        if "target_buffer_inr" not in {r[1] for r in c.execute("PRAGMA table_info(routes)")}:
            c.execute("ALTER TABLE routes ADD COLUMN target_buffer_inr INTEGER")  # NULL = [deal] default


def q(sql, args=()):
    with closing(connect()) as c, c:
        return c.execute(sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with closing(connect()) as c, c:
        return c.execute(sql, args).lastrowid


def insert(table, row):
    cols = ", ".join(row)
    return run(f"INSERT INTO {table}({cols}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))


def route(rid):
    return one("SELECT * FROM routes WHERE id=?", (rid,))


def update_route(rid, **fields):
    run(f"UPDATE routes SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), rid))


def kv_get(k):
    r = one("SELECT v FROM kv WHERE k=?", (k,))
    return r["v"] if r else None


def kv_set(k, v):
    run("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def backup(dest):
    with closing(connect()) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
