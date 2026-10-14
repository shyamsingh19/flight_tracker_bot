"""Pure alert rules and message text. No I/O.

When you get an alert (every number is a [deal] key in config.toml; D = your date):

  🟢 GOOD / GREAT DEAL  D only. Price is in the cheapest good_percentile / great_percentile of all
                        prices seen for D±flex over the last baseline_days. Silent until min_samples
                        prices exist. Repeats only if GOOD becomes GREAT, or the price falls another
                        realert_abs_inr AND realert_pct below the last deal alert.
  📉 DROP               D only. Price is drop_abs_inr AND drop_pct below D's lowest in the last
                        drop_window_h hours.
  💡 CHEAPER DATE       A date other than D is cheaper than D's latest price (no older than
                        freshness_h) by alt_cheaper_abs_inr or alt_cheaper_pct, whichever is larger.
                        Once per date per alt_realert_h, unless the saving grows by alt_realert_abs_inr.
  🎯 TARGET             Any date. Price ≤ your target + buffer (/settarget id price buffer, else
                        target_buffer_inr). Repeats only on a further
                        realert_abs_inr AND realert_pct fall.

API calls: deciding costs nothing; rules only read prices already fetched by the schedule.
Only two things spend extra calls, and only after a rule has fired:
  - re-check (1 call): if recheck_unverified and the price was unverified or cached, it is fetched
    again and the alert is dropped unless the two agree within recheck_tolerance_pct;
  - burst (one call per date): a deal or target alert on D searches D±burst_flex_days at once, at most
    every burst_cooldown_h ([route_defaults]), and replaces the next buffer scan when it covers it.
So looser thresholds mean more messages and more re-checks/bursts; tighten them to save calls.
"""
import statistics
from datetime import date, timedelta

RANK = {"good": 1, "great": 2}


def grade(cfg, price, baseline):
    """(grade, percentile) of price within the season baseline; (None, None) below min_samples."""
    d = cfg["deal"]
    if len(baseline) < d["min_samples"]:
        return None, None
    pct = 100 * (sum(b < price for b in baseline) + 1) / (len(baseline) + 1)
    g = "great" if pct <= d["great_percentile"] else "good" if pct <= d["good_percentile"] else "normal"
    return g, pct


def further_drop(cfg, last, price):
    d = cfg["deal"]
    return last - price >= d["realert_abs_inr"] and (last - price) * 100 / last >= d["realert_pct"]


def decide(cfg, now, *, is_anchor, price, target, target_buffer=None, low24, d_latest, baseline, last_grade, last_target,
           last_alt, **_):
    """Rules that fire for a just-fetched price → [{"rule", "saving", "pct"}]."""
    d = cfg["deal"]
    out = []
    if is_anchor:
        g, pct = grade(cfg, price, baseline)
        if g in RANK and (last_grade is None or RANK[g] > RANK[last_grade[0]]
                          or further_drop(cfg, last_grade[1], price)):
            out.append({"rule": g, "saving": None, "pct": pct})
        if low24 is not None and low24 - price >= d["drop_abs_inr"] and (low24 - price) * 100 / low24 >= d["drop_pct"]:
            out.append({"rule": "drop", "saving": low24 - price})
    elif d_latest is not None:
        saving = d_latest - price
        if saving >= max(d["alt_cheaper_abs_inr"], d_latest * d["alt_cheaper_pct"] / 100) and (
                last_alt is None or now - last_alt[1] >= timedelta(hours=d["alt_realert_h"])
                or saving - last_alt[0] >= d["alt_realert_abs_inr"]):
            out.append({"rule": "alt", "saving": saving})
    buf = d["target_buffer_inr"] if target_buffer is None else target_buffer
    if target and price <= target + buf and (
            last_target is None or further_drop(cfg, last_target, price)):
        out.append({"rule": "target", "saving": target - price})
    return out


def inr(x):
    return f"₹{x:,.0f}"


def day_label(day):
    return date.fromisoformat(day).strftime("%a %d %b")


def gf_link(r, day):
    return (f'<a href="https://www.google.com/travel/flights?q=Flights%20from%20{r["origin"]}%20to%20'
            f'{r["destination"]}%20on%20{day}%20oneway&curr=INR&hl=en-IN">Google Flights</a>')


def flight_text(obs):
    return f"{obs['flight_no']} {obs['dep_local'][11:16]}"


def target_text(r, p):
    t = r["target_inr"]
    return f"below your target {inr(t)}" if p <= t else f"{inr(p - t)} above your target {inr(t)}"


def message(cfg, r, day, obs, fired, ctx, nearby=None):
    """One Telegram message (HTML) for every rule that fired on this observation."""
    rules = {a["rule"]: a for a in fired}
    head, p = f"{r['origin']}→{r['destination']}", obs["price_inr"]
    g = rules.get("great") or rules.get("good")
    lines = []
    if g:
        lo = (date.fromisoformat(r["anchor_date"]) - timedelta(days=r["flex_days"])).strftime("%d %b")
        hi = (date.fromisoformat(r["anchor_date"]) + timedelta(days=r["flex_days"])).strftime("%d %b")
        b = ctx["baseline"]
        lines += [f"🟢 {g['rule'].upper()} DEAL · {head} {day_label(day)} · {inr(p)}",
                  f"Cheapest {g['pct']:.0f}% of {len(b)} prices seen for {lo}–{hi} over "
                  f"{cfg['deal']['baseline_days']} days (median {inr(statistics.median(b))}) · "
                  f"lowest seen {inr(min(b))}"]
    elif "drop" in rules:
        lines.append(f"📉 {head} {day_label(day)} · {inr(p)} (24h low {inr(ctx['low24'])}, "
                     f"−{rules['drop']['saving'] * 100 / ctx['low24']:.1f}%)")
    elif "alt" in rules:
        lines.append(f"💡 {head} · {day_label(day)} is {inr(p)}, {inr(rules['alt']['saving'])} less than your "
                     f"date {day_label(r['anchor_date'])} ({inr(ctx['d_latest'])})")
    else:
        lines.append(f"🎯 {head} {day_label(day)} · {inr(p)}, {target_text(r, p)}")
    if g and "drop" in rules:
        lines.append(f"📉 {inr(rules['drop']['saving'])} below the 24h low {inr(ctx['low24'])}")
    if "target" in rules and len(rules) > 1:
        lines.append(f"🎯 {target_text(r, p)}")

    bits = [flight_text(obs)]
    if ctx["last_flight"] and ctx["last_flight"] != obs["flight_no"]:
        bits[0] += f" (flight changed, was {ctx['last_flight']})"
    if obs["bags"] is not None:
        bits.append(f"{obs['bags']} checked bag{'s' * (obs['bags'] != 1)}")
    if ctx["prev_low"] is not None and p < ctx["prev_low"]:
        bits.append("lowest ever")
    if r["target_inr"]:
        bits.append(f"target {inr(r['target_inr'])}")
    bits.append(gf_link(r, day))
    lines.append(" · ".join(bits))

    if nearby:
        best = min(nearby, key=lambda x: x[1]["price_inr"])
        cells = [f"{'★ ' if n is best and n[1]['price_inr'] < p else ''}"
                 f"{date.fromisoformat(n[0]).strftime('%a %d')} {inr(n[1]['price_inr'])}"
                 f"{' (' + flight_text(n[1]) + ')' if n is best and n[1]['price_inr'] < p else ''}"
                 for n in nearby]
        lines += ["Nearby dates, checked just now:", " · ".join(cells)]
    if g and ctx["route_normal"]:
        diff = (statistics.median(ctx["baseline"]) / ctx["route_normal"] - 1) * 100
        lines.append(f"Your route's all-time normal: {inr(ctx['route_normal'])} (this season {diff:+.0f}%)")
    return "\n".join(lines)
