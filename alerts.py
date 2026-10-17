"""Pure alert rules and message text. No I/O.

Goal: get the cheapest fare for your date D, and tell you when it is worth booking.
A route watches exactly one date. To watch another day, track it as its own route.
Every number below is a [deal] key in config.toml unless noted. Rules only read prices that
were already fetched. The clock is never read inside this module.

DEFINITIONS
  price        Cheapest all-in INR fare for a date (1 adult, one-way, economy) as returned by
               the search. Indicative only: confirm the final payable amount on the booking page.
  valid        Price is a positive integer, the date departs more than min_lead_h after now,
               and, if a re-check was required, it passed (see RE-CHECK). Invalid prices are
               ignored everywhere: not stored as lowest, not alerted, not in the baseline.
  fresh        Fetched within freshness_h of now. A date's "current price" is its newest fresh
               valid price. A date with no fresh price is skipped this scan.
  step(p, a, b)  max(a, b × p). "Meaningfully lower than p" means ≤ p − step(p, a, b).
               realert step = step(p, realert_abs_inr, realert_pct)
  lowest seen  Lowest valid price this system has recorded for D, from any fetch, since
               first_seen. It is the lowest WE recorded, never a historical all-time low, and
               messages say so ("lowest we've recorded since <first_seen>").
  baseline     D's prices from scheduled checks in the last baseline_days, one sample per
               scheduled slot. Manual checks and re-checks never enter.

RULES  (D = your date)
  🟢 GOOD / GREAT  Silent until the baseline has min_samples samples.
                   GOOD:  current price is strictly below the baseline's good_percentile AND
                          meaningfully lower (realert step) than the baseline median.
                   GREAT: same test with great_percentile.
                   The strict "below" plus the median gap means a flat price is never a deal.
  📉 NEW LOW       After min_samples. Current price is strictly below lowest seen.
                   (Replaces DROP: no false alarm when a spike recovers to a normal price.)
  🎯 TARGET        Routes with a target (/settarget price [buffer]; buffer defaults
                   to target_buffer_inr).
                   HIT:  price ≤ target.     Message: "at/below your target".
                   NEAR: price ≤ target + buffer.   Message: "within ₹X of your target".
                   The message always names the date.
  ⏰ BOOK BY       Once, on the first scan at or after deadline_days before D. Sends D's current
                   price and lowest seen regardless of every other rule, because fares usually
                   rise near departure. If D has no fresh price, the message says so.

RE-ALERT STATE  (one record per rule × date: last_alert_price, last_level)
  Applies to GOOD/GREAT, NEW LOW and TARGET. The same rule and date alerts again only if:
    - the level rises (GOOD → GREAT, NEAR → HIT), or
    - the price is meaningfully lower (realert step) than last_alert_price, or
    - the deal went away and came back: once a fresh price ≥ last_alert_price + realert step
      is seen, the record is cleared, so the next qualifying price alerts as new.
  BOOK BY fires once.

ONE MESSAGE PER SCAN
  All rules are evaluated together and produce at most one message. Headline by priority:
    🎯 HIT > 🟢 GREAT > ⏰ BOOK BY > 🎯 NEAR > 🟢 GOOD > 📉 NEW LOW
  Other rules that fired are listed as lines under the headline. Every message shows the date,
  price, airline (if known) and "indicative, confirm on booking page".

RE-CHECK  (1 call, only after a rule fired, only for prices that will appear in a message)
  Runs if (recheck_unverified and the price was unverified or cached), OR the price is more
  than suspicious_pct below lowest seen (even if verified).
    - Re-check higher than the original by more than recheck_tolerance_pct
      → the original is invalid; drop it.
    - Re-check close or lower → use the re-checked price and re-run the rules on it
      (a lower price can raise the level).
    - Re-check call fails → send the alert marked ⚠️ unverified, but do not update lowest
      seen or re-alert state with that price.

API CALLS
  Deciding costs nothing. The only extra call is at most 1 re-check per alerted price.

CONFIG CHANGES vs previous version
  Added:   min_lead_h, deadline_days, suspicious_pct
  Removed: drop_abs_inr, drop_pct, drop_window_h (DROP replaced by NEW LOW);
           nearby dates (flex, buffer scan, alternative watch, burst, CHEAPER DATE): one date per route
  Changed: realert_abs_inr / realert_pct are combined as max(), and also set GOOD/GREAT's
           minimum gap below the baseline median.

DELIVERY
  Messages go to the route's owner as Telegram text (bot.send), and are recorded in alerts /
  alert_state only after they were sent. Phone calls are not implemented yet.
"""
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

RANK = {None: 0, "good": 1, "near": 1, "great": 2, "hit": 2}
REALERT = ("deal", "new_low", "target")  # rules that use RE-ALERT STATE
PRIORITY = [("target", "hit"), ("deal", "great"), ("book_by", None), ("target", "near"), ("deal", "good"),
            ("new_low", None)]


def step(p, a, pct):
    return max(a, pct * p / 100)


def realert_step(cfg, p):
    return step(p, cfg["deal"]["realert_abs_inr"], cfg["deal"]["realert_pct"])


def percentile(xs, pct):
    """Linear-interpolated value at pct (0–100) of xs."""
    xs = sorted(xs)
    k = (len(xs) - 1) * pct / 100
    f = int(k)
    return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)


def grade(cfg, price, baseline):
    """GOOD/GREAT test → {"level", "pct", "cut", "median", "n"}, or None (not a deal, or below min_samples)."""
    d = cfg["deal"]
    if len(baseline) < d["min_samples"]:
        return None
    med = statistics.median(baseline)
    if price > med - realert_step(cfg, med):
        return None
    for level, pct in (("great", d["great_percentile"]), ("good", d["good_percentile"])):
        if price < (cut := percentile(baseline, pct)):
            return {"level": level, "pct": pct, "cut": cut, "median": med, "n": len(baseline)}
    return None


def decide(cfg, *, day, price, baseline, lowest, target, target_buffer, states, **_):
    """Rules for one fresh valid price of `day` → (fired, clear).
    fired: [{"rule", "level", "saving", ...message details}]; clear: rules whose state for `day`
    must be deleted because the deal went away. `states` is db.alert_states() for the route."""
    d = cfg["deal"]
    clear = [rule for (rule, dd), s in states.items() if dd == day and rule in REALERT
             and price >= s["price_inr"] + realert_step(cfg, s["price_inr"])]
    st = {rule: s for (rule, dd), s in states.items() if dd == day and rule not in clear}

    def again(rule, level):
        s = st.get(rule)
        return (s is None or RANK[level] > RANK[s["level"]]
                or price <= s["price_inr"] - realert_step(cfg, s["price_inr"]))

    out = []
    if len(baseline) >= d["min_samples"]:
        g = grade(cfg, price, baseline)
        if g and again("deal", g["level"]):
            out.append({"rule": "deal", "saving": round(g["median"] - price), **g})
        if lowest is not None and price < lowest and again("new_low", None):
            out.append({"rule": "new_low", "level": None, "saving": lowest - price})
    if target:
        buf = d["target_buffer_inr"] if target_buffer is None else target_buffer
        level = "hit" if price <= target else "near" if price <= target + buf else None
        if level and again("target", level):
            out.append({"rule": "target", "level": level, "saving": target - price, "target": target})
    return out, clear


def book_by_due(cfg, today, anchor_date, states):
    """⏰ BOOK BY fires once, on the first scan at or after deadline_days before D."""
    d = date.fromisoformat(anchor_date)
    return today >= d - timedelta(days=cfg["deal"]["deadline_days"]) and ("book_by", anchor_date) not in states


def needs_recheck(cfg, obs, lowest):
    d = cfg["deal"]
    return bool((d["recheck_unverified"] and (obs["price_status"] != "verified" or obs["cache_hit"]))
                or (lowest and obs["price_inr"] < lowest * (1 - d["suspicious_pct"] / 100)))


def priority(hit):
    return PRIORITY.index((hit["rule"], hit.get("level"))), hit.get("price") or 0


# ---------------------------------------------------------------- message text

def inr(x):
    return f"₹{x:,.0f}"


def day_label(day):
    return date.fromisoformat(day).strftime("%a %d %b")


def route_name(r):
    """What users see instead of the database id: 'HYD → DEL · Sun 20 Dec' (+ flight / direct if set)."""
    extra = f" · {r['flight_no']}" if r["flight_no"] else " · direct" if r["max_stops"] == 0 else ""
    return f"{r['origin']} → {r['destination']} · {day_label(r['anchor_date'])}{extra}"


def gf_link(r, day):
    return (f'<a href="https://www.google.com/travel/flights?q=Flights%20from%20{r["origin"]}%20to%20'
            f'{r["destination"]}%20on%20{day}%20oneway&curr=INR&hl=en-IN">Google Flights</a>')


def flight_text(obs):
    return f"{obs['flight_no']} {obs['dep_local'][11:16]}"


class Flight(str):
    """A message for one route's owner (price alerts, expiry): goes to Flight.to only. Plain strings are admin notices.
    `record` = (alerts lines, alert_state rows) for db.record_message once it was sent."""
    to = None
    record = None


def for_owner(r, text):
    m = Flight(text)
    m.to = r["owner_id"]
    return m


def lowest_text(h, tz):
    since = datetime.fromisoformat(h["first_seen"]).astimezone(tz)
    return f"lowest we've recorded since {since:%d %b} {inr(h['lowest'])}"


def line(cfg, h, tz):
    day, p, lv = day_label(h["day"]), h["price"], h.get("level")
    if h["rule"] == "target":
        t = h["target"]
        text = f"🎯 TARGET {day} · {inr(p)}, " + (f"at/below your target {inr(t)}" if lv == "hit"
                                                  else f"within {inr(p - t)} of your target {inr(t)}")
    elif h["rule"] == "deal":
        text = (f"🟢 {lv.upper()} DEAL {day} · {inr(p)}, below the {h['pct']:g}th percentile {inr(h['cut'])} "
                f"of {h['n']} prices in {cfg['deal']['baseline_days']} days (median {inr(h['median'])})")
    elif h["rule"] == "new_low":
        text = f"📉 NEW LOW {day} · {inr(p)}, previous {lowest_text(h, tz)}"
    else:
        text = (f"⏰ BOOK BY {day} is {h['days_left']} days away: "
                + (f"now {inr(p)}" if p is not None else "no fresh price right now")
                + (f", {lowest_text(h, tz)}" if h["lowest"] is not None else "")
                + ". Fares usually rise near departure.")
    return text + (" ⚠️ unverified" if h.get("unverified") else "")


def message(cfg, r, hits):
    """One Telegram message (HTML) for every rule that fired in one scan; hits sorted by priority()."""
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    head = hits[0]
    lines = [f"✈️ {route_name(r)}"] + [line(cfg, h, tz) for h in hits]
    bits = []
    if o := head.get("obs"):
        bits.append(flight_text(o))
        if o["bags"] is not None:
            bits.append(f"{o['bags']} checked bag{'s' * (o['bags'] != 1)}")
    bits += [gf_link(r, head["day"]), "indicative, confirm on booking page"]
    lines.append(" · ".join(bits))
    return for_owner(r, "\n".join(lines))
