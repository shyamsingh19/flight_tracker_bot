"""python -m unittest — no network, no Telegram, 0 Ignav calls."""
import json
import re
import tempfile
import tomllib
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import alerts
import budget
import checker
import config
import db
import providers

HERE = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
D = "2026-12-20"


def at(day, hh, mm=0):
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}").replace(tzinfo=IST).astimezone(UTC)


def offer(price, day=D, flight="6E2134", dep="06:10", status="verified", bags=1, stops=0):
    return {"price_inr": price, "price_status": status, "airline": flight[:2], "flight_no": flight,
            "dep_local": f"{day}T{dep}:00", "stops": stops, "bags": bags}


class Fake:
    """Stands in for providers.ignav_search. prices: date → int | list[offer] | list of those (one per call)."""
    def __init__(self):
        self.prices, self.calls, self.error, self.cache = {}, [], None, False

    def __call__(self, cfg, o, d, day, stops, bags):
        self.calls.append(day)
        if self.error:
            raise self.error
        p = self.prices.get(day, 6000)
        if isinstance(p, list) and p and isinstance(p[0], (int, list)):
            p = p.pop(0) if len(p) > 1 else p[0]
        return (p if isinstance(p, list) else [offer(p, day)]), self.cache, 1


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.PATH = str(Path(self.tmp.name) / "t.db")
        db.init()
        self.cfg = config.load(HERE / "config.toml")
        self.cfg["calibration"]["enabled"] = False
        self.fake = checker.search = Fake()

    def tearDown(self):
        checker.search = providers.ignav_search
        self.tmp.cleanup()

    def route(self, created=None, **kw):
        r = {"origin": "HYD", "destination": "DEL", "anchor_date": D, "flex_days": 5, "calibration": 0,
             "status": "active", "created_at": db.iso(created or at("2026-12-01", 7))} | kw
        return db.insert("routes", r)

    def alerts_of(self, rule):
        return db.q("SELECT * FROM alerts WHERE rule=? ORDER BY id", (rule,))

    def check(self, rid, day, price, now, kind="anchor"):
        self.fake.prices[day] = price
        return checker.check(self.cfg, db.route(rid), day, kind, now)


class TestConfig(Base):
    def test_shipped_file_matches_schema(self):
        raw = tomllib.loads((HERE / "config.toml").read_text())
        self.assertEqual({s: set(v) for s, v in raw.items()}, {s: set(v) for s, v in config.SCHEMA.items()})

    def test_every_key_is_used_in_code(self):
        src = "".join(p.read_text() for p in HERE.glob("*.py") if p.name not in ("config.py", Path(__file__).name))
        cfg_src = (HERE / "config.py").read_text()
        src += cfg_src[cfg_src.index("\n}\n"):]  # config.py minus the SCHEMA block
        unused = [k for keys in config.SCHEMA.values() for k in keys if f'"{k}"' not in src]
        self.assertEqual(unused, [])

    def test_no_magic_numbers_for_tunables(self):
        # a tunable default appearing as a literal in logic would be a hard-coded copy
        for f in ["checker.py", "alerts.py", "budget.py", "bot.py"]:
            for n in ["0.002", "115", "750", "1000", "26", "300"]:
                self.assertIsNone(re.search(rf"(?<![\w.\"]){re.escape(n)}(?![\w.])", (HERE / f).read_text()), (f, n))

    def write(self, text):
        p = Path(self.tmp.name) / "c.toml"
        p.write_text(text)
        return p

    def test_invalid_file_rejected_with_key(self):
        for bad, needle in [('[schedule]\nanchor_every_min = "x"', "anchor_every_min"),
                            ("[schedule]\nactive_hours = \"23-08\"", "active_hours"),
                            ("[deal]\nnope = 1", "nope"),
                            ("[schedule]\nbuffer_scan_every_h = 12", "buffer_scan_every_h"),
                            ("[budget]\nwarn_at_pct = [50, true]", "warn_at_pct")]:
            with self.assertRaises(config.ConfigError) as e:
                config.load(self.write(bad))
            self.assertIn(needle, str(e.exception))

    def test_projected_cost_follows_config(self):
        self.route()
        now = at("2026-12-01", 9)
        base = checker.projected(self.cfg, now)
        for sec, key, val in [("schedule", "anchor_every_min", 120), ("schedule", "buffer_scan_every_h", 0)]:
            cfg = json.loads(json.dumps(self.cfg))
            cfg["schedule"]["active_hours"] = tuple(cfg["schedule"]["active_hours"])
            cfg[sec][key] = val
            self.assertLess(checker.projected(cfg, now)[1], base[1], key)
        db.update_route(1, flex_days=3)
        self.assertLess(checker.projected(self.cfg, now)[1], base[1])


class TestCost(Base):
    def test_calls_per_day(self):
        c = self.cfg
        self.assertEqual(config.calls_per_day(c, 5, 60, with_alt=False), 26)
        self.assertEqual(config.calls_per_day(c, 5, 60), 31)
        self.assertEqual(config.calls_per_day(c, 5, 30), 46)
        self.assertEqual(config.calls_per_day(c, 5, 120), 23)
        self.assertEqual(len(config.job_slots(c, 60, 3)["anchor"][0]), 6)
        lo, hi = (config.monthly_inr(c, config.calls_per_day(c, 5, 60, with_alt=a)) for a in (False, True))
        self.assertAlmostEqual(lo, 179.4)
        self.assertAlmostEqual(hi, 213.9)

    def test_far_out_route_is_slower(self):
        self.route(anchor_date="2027-03-01")
        lo, _ = checker.projected(self.cfg, at("2026-12-01", 9))
        self.assertLess(lo, 100)


class TestSchedule(Base):
    def test_anchor_slots_only_in_active_hours(self):
        for every, n in [(60, 16), (30, 31)]:
            slots = config.job_slots(self.cfg, every)["anchor"][0]
            self.assertEqual(len(slots), n)
            self.assertEqual((slots[0], slots[-1]), (8 * 60, 23 * 60))

    def test_day_cadence(self):
        self.route()
        run = lambda h, m=0: (self.fake.calls.clear(), checker.run_due(self.cfg, at("2026-12-01", h, m)),  # noqa: E731
                              list(self.fake.calls))[2]
        self.assertEqual(run(2), [])                 # night: nothing
        calls = run(8)                               # D, then the buffer scan
        self.assertEqual(calls[0], D)
        self.assertEqual(len(calls), 11)
        self.assertEqual(run(8, 30), [])
        self.assertEqual(run(9), [D])
        db.update_route(1, alt_date="2026-12-21")
        self.assertEqual(run(11), [D, "2026-12-21"])
        self.assertEqual(run(23, 0)[0], D)                # D, plus the alternative at 23:00
        self.assertEqual(run(23, 30), [])

    def test_buffer_dates(self):
        today = date(2026, 12, 17)
        self.assertEqual(checker.buffer_dates(D, 5, today),
                         ["2026-12-17", "2026-12-18", "2026-12-19", "2026-12-21", "2026-12-22", "2026-12-23",
                          "2026-12-24", "2026-12-25"])
        self.assertEqual(len(checker.buffer_dates(D, 2, date(2026, 12, 1))), 4)
        self.assertEqual(checker.buffer_dates(D, 0, date(2026, 12, 1)), [])

    def test_expiry(self):
        rid = self.route()
        checker.run_due(self.cfg, at("2026-12-21", 9))
        self.assertEqual(db.route(rid)["status"], "expired")
        rid = self.route(flight_no="6E2134")
        self.check(rid, D, [offer(5000, dep="10:00")], at("2026-12-20", 8))
        checker.run_due(self.cfg, at("2026-12-20", 9))
        self.assertEqual(db.route(rid)["status"], "active")
        checker.run_due(self.cfg, at("2026-12-20", 10, 1))
        self.assertEqual(db.route(rid)["status"], "expired")


class TestAltWatch(Base):
    def test_pick_and_release(self):
        rid = self.route()
        now = at("2026-12-01", 8)
        self.fake.prices = {D: 5000, "2026-12-21": 4900, "2026-12-22": 5150}
        checker.run_due(self.cfg, now)
        self.assertEqual(db.route(rid)["alt_date"], "2026-12-21")
        self.fake.prices["2026-12-21"] = 5500
        checker.job_alt(self.cfg, db.route(rid), at("2026-12-01", 11))
        self.assertEqual(db.route(rid)["alt_date"], "2026-12-22")  # 5150 is within ₹200 of 5000
        self.fake.prices["2026-12-22"] = 5300
        checker.check(self.cfg, db.route(rid), "2026-12-22", "alt", at("2026-12-01", 14))
        checker.pick_alt(self.cfg, rid, at("2026-12-01", 14))
        self.assertIsNone(db.route(rid)["alt_date"])

    def test_cheaper_alternative_alert_once_per_window(self):
        rid = self.route()
        alt = "2026-12-21"
        self.check(rid, D, 5000, at("2026-12-01", 8))
        self.assertTrue(self.check(rid, alt, 4500, at("2026-12-01", 8, 1), "scan"))
        self.assertFalse(self.check(rid, alt, 4500, at("2026-12-01", 11), "alt"))
        self.assertTrue(self.check(rid, alt, 4350, at("2026-12-01", 14), "alt"))  # saving grew by ₹150
        self.check(rid, D, 5000, at("2026-12-02", 14))
        self.assertTrue(self.check(rid, alt, 4500, at("2026-12-02", 14, 1), "alt"))  # 24 h later
        self.assertEqual(len(self.alerts_of("alt")), 3)

    def test_stale_anchor_price_not_compared(self):
        rid = self.route()
        self.check(rid, D, 5000, at("2026-12-01", 8))
        self.assertFalse(self.check(rid, "2026-12-21", 4000, at("2026-12-02", 11), "alt"))  # D is 27 h old


class TestRules(Base):
    def test_bouncing_price_alerts_once(self):
        rid = self.route()
        for h, p in zip(range(9, 13), [4900, 4500, 4900, 4500]):
            self.check(rid, D, p, at("2026-12-01", h))
        self.assertEqual(len(self.alerts_of("drop")), 1)

    def test_flight_changed_note(self):
        rid = self.route()
        self.check(rid, D, [offer(5000, flight="AI559")], at("2026-12-01", 9))
        self.check(rid, D, [offer(4500, flight="AI559")], at("2026-12-01", 10))
        msg = self.check(rid, D, [offer(4000, flight="QP1402")], at("2026-12-01", 11))
        self.assertIn("was AI559", msg[0])
        self.assertIn("lowest ever", msg[0])

    def test_target_buffer(self):
        rid = self.route(target_inr=4500, flex_days=0)
        self.assertFalse(self.check(rid, D, 4500 + self.cfg["deal"]["target_buffer_inr"] + 1, at("2026-12-01", 9)))
        msg = self.check(rid, D, 4600, at("2026-12-01", 10))
        self.assertIn("₹100 above your target ₹4,500", msg[0])
        db.update_route(rid, target_buffer_inr=0)  # per-route buffer beats the config default
        self.assertFalse(self.check(rid, D, 4501, at("2026-12-01", 11)))

    def test_set_target(self):
        rid = self.route()
        self.assertIn("❓", bot.set_target(db.route(rid), "abc"))
        self.assertIn("₹8,000", bot.set_target(db.route(rid), "8000"))
        self.assertEqual(db.route(rid)["target_inr"], 8000)
        self.assertIn("at or below ₹8,500", bot.set_target(db.route(rid), "₹8,000 +500"))
        self.assertEqual(db.route(rid)["target_buffer_inr"], 500)
        bot.set_target(db.route(rid), "OFF")
        self.assertIsNone(db.route(rid)["target_inr"])

    def test_below_target_realert(self):
        rid = self.route(target_inr=4500, flex_days=0)
        self.assertTrue(self.check(rid, D, 4400, at("2026-12-01", 9)))
        self.assertFalse(self.check(rid, D, 4350, at("2026-12-01", 10)))
        self.assertTrue(self.check(rid, D, 4200, at("2026-12-01", 11)))
        self.assertEqual(len(self.alerts_of("target")), 2)

    def test_recheck_unverified(self):
        rid = self.route()
        self.check(rid, D, 5000, at("2026-12-01", 9))
        self.fake.calls.clear()
        self.fake.prices[D] = [[offer(4500, status="unverified")], [offer(4800)]]  # re-check disagrees
        self.assertEqual(checker.check(self.cfg, db.route(rid), D, "anchor", at("2026-12-01", 10)), [])
        self.assertEqual(len(self.fake.calls), 2)
        self.fake.prices[D] = [[offer(4000, status="unverified")], [offer(4050)]]  # within 2%
        msg = checker.check(self.cfg, db.route(rid), D, "anchor", at("2026-12-01", 11))
        self.assertIn("₹4,050", msg[0])

    def test_grade(self):
        base = list(range(5000, 6000, 50))
        self.assertEqual(alerts.grade(self.cfg, 4900, base)[0], "great")
        self.assertEqual(alerts.grade(self.cfg, 5100, base)[0], "good")
        self.assertEqual(alerts.grade(self.cfg, 5500, base)[0], "normal")
        self.assertEqual(alerts.grade(self.cfg, 4900, base[:19]), (None, None))

    def test_baseline_window(self):
        rid = self.route(flex_days=2)
        now = at("2026-12-10", 9)
        for day, when in [(D, now - timedelta(days=31)), ("2026-12-25", now), ("2026-12-19", now)]:
            db.insert("observations", {"route_id": rid, "depart_date": day, "kind": "scan", "observed_at": db.iso(when),
                                       "provider": "ignav", "ok": 1, "price_inr": 5000})
        self.assertEqual(checker.context(self.cfg, db.route(rid), D, now)["baseline"], [5000])

    def test_good_deal_burst(self):
        rid = self.route()
        now = at("2026-12-01", 15)
        for i in range(25):  # season baseline
            db.insert("observations", {"route_id": rid, "depart_date": D, "kind": "anchor", "ok": 1,
                                       "observed_at": db.iso(now - timedelta(days=2, hours=i)), "provider": "ignav",
                                       "price_inr": 6000 + 10 * i})
        self.fake.prices = {"2026-12-21": 4300}
        msg = self.check(rid, D, 5000, now)
        self.assertEqual(len(self.fake.calls), 11)  # D + 10 nearby dates, one message
        self.assertEqual(len(msg), 1)
        self.assertIn("GREAT DEAL", msg[0])
        self.assertIn("★ Mon 21", msg[0])
        self.assertEqual(db.route(rid)["skip_next_scan"], 1)
        self.fake.calls.clear()
        self.assertTrue(self.check(rid, D, 4800, at("2026-12-01", 16)))  # re-alert, but in cooldown → no burst
        self.assertEqual(self.fake.calls, [D])
        self.fake.calls.clear()
        checker.run_due(self.cfg, at("2026-12-02", 8))  # the scan the burst replaced is skipped
        self.assertEqual(self.fake.calls, [D, "2026-12-21"])


class TestMoneyAndFailures(Base):
    def test_hard_stop(self):
        self.cfg["budget"]["ignav_free_calls"] = 0
        self.cfg["budget"]["monthly_cap_inr"] = 1  # hard stop ₹0.90 = 3 calls at ₹0.23
        rid = self.route()
        msgs = []
        for h in range(9, 14):
            msgs += self.check(rid, D, 6000, at("2026-12-01", h))
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(sum("stopped" in m for m in msgs), 1)
        self.assertTrue(any("50%" in m for m in msgs))

    def test_free_calls_cost_nothing(self):
        rid = self.route()
        self.check(rid, D, 6000, at("2026-12-01", 9))
        self.assertEqual(budget.month_spend(self.cfg, at("2026-12-01", 9)), 0)
        self.assertEqual(budget.free_left(self.cfg), 999)

    def test_failure_notify_after_three(self):
        rid = self.route()
        self.fake.error = providers.ProviderError("HTTP 401: bad key", 401)
        msgs = [self.check(rid, D, 0, at("2026-12-01", h, m)) for h, m in ((9, 0), (9, 15), (9, 30), (9, 45))]
        self.assertEqual([bool(m) for m in msgs], [False, False, True, False])
        self.fake.error = None
        self.check(rid, D, 6000, at("2026-12-01", 13))
        self.assertEqual(db.route(rid)["consecutive_failures"], 0)

    def test_429_pauses(self):
        rid = self.route()
        self.fake.error = providers.ProviderError("HTTP 429", 429)
        self.check(rid, D, 0, at("2026-12-01", 9))
        self.fake.error = None
        self.check(rid, D, 6000, at("2026-12-01", 9, 30))
        self.assertEqual(len(self.fake.calls), 1)
        self.check(rid, D, 6000, at("2026-12-01", 10, 1))
        self.assertEqual(len(self.fake.calls), 2)


class TestProvider(Base):
    def test_filters(self):
        r = {"airline": None, "flight_no": None, "time_start": 6, "time_end": 12, "min_bags": 1}
        offers = [offer(4000, dep="05:00"), offer(4100, dep="07:00", bags=0), offer(4200, dep="13:00"),
                  offer(4300, dep="11:00", stops=1), offer(4400, dep="12:00")]
        self.assertEqual(providers.cheapest(offers, r, 0)["price_inr"], 4400)
        self.assertEqual(providers.cheapest(offers, r, 1)["price_inr"], 4300)
        self.assertIsNone(providers.cheapest(offers, r | {"airline": "AI"}, 1))
        self.assertEqual(providers.cheapest([offer(4000, dep="07:00", bags=None)], r, 0)["price_inr"], 4000)

    def test_parse_fixture(self):
        data = json.loads((HERE / "fixtures" / "sample_oneway.json").read_text())
        offers, hit = providers.parse(data, self.cfg)
        self.assertEqual(len(offers), 5)
        self.assertEqual(offers[3]["stops"], 1)
        self.assertEqual(offers[0]["flight_no"], "6E2134")

    def test_malformed(self):
        orig = providers.post
        try:
            for bad in [{}, {"itineraries": [{"price": {}}]}, {"itineraries": [{"price": {"amount": 1, "currency": "USD",
                        "status": "verified"}, "outbound": {"segments": []}}]}]:
                providers.post = lambda cfg, body, bad=bad: (bad, 1)
                with self.assertRaises(providers.ProviderError):
                    providers.ignav_search(self.cfg, "HYD", "DEL", D, 1, None)
        finally:
            providers.post = orig


class TestCalibration(Base):
    def series(self, dips_at_min):
        out = []
        start = datetime(2026, 12, 1, tzinfo=IST)
        for day in range(14):
            for m in range(8 * 60, 23 * 60 + 1, 30):
                t = start + timedelta(days=day, minutes=m)
                out.append((t, 5000 if day % 2 == 0 and m == dips_at_min else 6000, False))
        return out

    def test_missed_half_hour_dips_recommend_30(self):
        verdict, text = checker.calibration_report(self.cfg, self.series(10 * 60 + 30), 5)
        self.assertIn("move D to every 30 min", verdict)
        self.assertIn("7 drop events", text)

    def test_hourly_dips_step_down_or_stay(self):
        verdict, _ = checker.calibration_report(self.cfg, self.series(10 * 60), 5)  # 10:00 is on the 2 h grid too
        self.assertIn("step D down to every 120 min", verdict)
        verdict, _ = checker.calibration_report(self.cfg, self.series(11 * 60), 5)  # 11:00: hourly only
        self.assertIn("stay at 60 min", verdict)

    def test_not_enough_evidence(self):
        verdict, _ = checker.calibration_report(self.cfg, self.series(10 * 60 + 30)[:31 * 6], 5)
        self.assertIn("not enough evidence", verdict)

    def test_calibration_route_runs_every_30_min(self):
        self.cfg["calibration"]["enabled"] = True
        rid = self.route(calibration=1, flex_days=0)
        checker.run_due(self.cfg, at("2026-12-01", 9))
        checker.run_due(self.cfg, at("2026-12-01", 9, 30))
        self.assertEqual(len(self.fake.calls), 2)
        self.assertIn("Calibration report", checker.run_due(self.cfg, at("2026-12-15", 9))[-1])  # after 14 days
        self.assertEqual(checker.run_due(self.cfg, at("2026-12-16", 9)), [])
        self.assertTrue(db.kv_get(f"calib_reported:{rid}"))


try:
    import bot
except ImportError:  # python-telegram-bot not installed (system python); the venv has it
    bot = None


@unittest.skipUnless(bot, "python-telegram-bot not installed")
class TestGuidedFlow(Base):
    def test_airport_names(self):
        for text, code in [("Hyderabad", "HYD"), ("New delhi", "DEL"), (" new  Delhi ", "DEL"), ("goa", "GOI"),
                           ("bom", "BOM"), ("Bangalore airport", "BLR"), ("Atlantis", None)]:
            self.assertEqual(bot.airport(text), code, text)

    def test_parse_day(self):
        today = date(2026, 10, 13)
        for text, want in [("6 november", "2026-11-06"), ("Nov 6", "2026-11-06"), ("6th Nov", "2026-11-06"),
                           ("06.11", "2026-11-06"), ("6/11/2026", "2026-11-06"), ("2026-11-06", "2026-11-06"),
                           ("5 march", "2027-03-05"), ("tomorrow", "2026-10-14"), ("12 oct 2027", "2027-10-12")]:
            self.assertEqual(bot.parse_day(text, today).isoformat(), want, text)
        for bad in ["someday", "32 nov", "6 smarch"]:
            with self.assertRaises(ValueError):
                bot.parse_day(bad, today)

    def test_search_results_and_track_buttons(self):
        self.fake.prices["2026-11-06"] = [offer(9922, "2026-11-06", "6E668", stops=1),
                                          offer(11200, "2026-11-06", "AI2880", dep="17:45") | {"arr_local": "2026-11-06T20:00:00"}]
        s = {"o": "HYD", "d": "DEL", "day": "2026-11-06", "direct": False}
        s["offers"] = bot.run_search(self.cfg, "HYD", "DEL", "2026-11-06", at("2026-10-13", 15))
        self.assertEqual(budget.calls_used(), 1)
        text, kb = bot.results_view(self.cfg, s)
        self.assertIn("1 stop", text)
        self.assertIn("17:45 → 20:00", text)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "s:trk")
        s["direct"] = True
        text, kb = bot.results_view(self.cfg, s)
        self.assertNotIn("6E668", text)
        route, _ = bot.parse_track(self.cfg, ["HYD", "DEL", "2026-11-06", "stops=0"], date(2026, 10, 13))
        self.assertIn("✅ Tracking #1", bot.save_route(self.cfg, route, False, at("2026-10-13", 15)))
        _, kb = bot.results_view(self.cfg, s)
        self.assertEqual((kb.inline_keyboard[0][0].text, kb.inline_keyboard[0][0].callback_data),
                         ("✅ Tracking #1", "menu:alerts"))
        s["direct"] = False  # all-flights watch of the same date is a different route
        self.assertEqual(bot.results_view(self.cfg, s)[1].inline_keyboard[0][0].callback_data, "s:trk")

    def test_alerts_view_buttons(self):
        self.route()
        text, kb = bot.alerts_view(self.cfg, at("2026-12-01", 9))
        self.assertIn("#1 HYD → DEL", text)
        self.assertIn("← your date", text)
        self.assertEqual([b.callback_data for b in kb.inline_keyboard[0]], ["r:deleted:1", "t:1", "r:paused:1"])

    def test_app_wiring_builds(self):
        app = bot.build_app(self.cfg, "123:abc")
        self.assertEqual(len(app.handlers[-1]), 1)  # the authorization gate
        self.assertEqual({j.name for j in app.job_queue.jobs()}, {"tick", "heartbeat", "summary", "backup"})


if __name__ == "__main__":
    unittest.main()
