"""Ignav one-way search → offers. The only data source (plan §2); swap this file to change provider."""
import json
import os
import random
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

URL = "https://ignav.com/api/fares/one-way"
FIXTURES = Path(__file__).with_name("fixtures")


class ProviderError(Exception):
    def __init__(self, message, status=None, attempts=1):
        super().__init__(message)
        self.message, self.status, self.attempts = message, status, attempts


def body(cfg, origin, dest, day, max_stops, min_bags):
    s = cfg["search"]
    b = {"origin": origin, "destination": dest, "departure_date": day, "adults": s["adults"],
         "cabin_class": s["cabin_class"], "max_stops": max_stops,
         "allow_self_transfer": s["allow_self_transfer"], "market": s["market"]}
    if min_bags:
        b["min_checked_bags"] = min_bags
    return b


def post(cfg, payload):
    """POST to Ignav → (raw dict, attempts). Retries network errors and 5xx only; never a 429."""
    p = cfg["provider"]
    req = Request(URL, data=json.dumps(payload).encode(), headers={
        "X-Api-Key": os.environ.get("IGNAV_API_KEY", ""), "Content-Type": "application/json",
        "Accept": "application/json"})
    for attempt in range(1, p["retries"] + 2):
        try:
            with urlopen(req, timeout=p["timeout_s"]) as resp:
                raw = resp.read()
            break
        except HTTPError as e:
            if e.code < 500 or attempt > p["retries"]:
                raise ProviderError(f"HTTP {e.code}: {_error_text(e)}", e.code, attempt) from None
        except (URLError, OSError) as e:
            if attempt > p["retries"]:
                raise ProviderError(f"network error: {e}", None, attempt) from None
        time.sleep(p["retry_backoff_s"])
    try:
        return json.loads(raw), attempt
    except ValueError:
        raise ProviderError("malformed response: not JSON", None, attempt) from None


def _error_text(e):
    try:
        return json.load(e)["error"]["message"]
    except Exception:
        return str(e.reason)


def parse(data, cfg):
    """Ignav FareSearchResponseModel → (offers, cache_hit). Raises ValueError/KeyError/TypeError if malformed."""
    offers = []
    for it in data["itineraries"]:
        price, segs = it["price"], it["outbound"]["segments"]
        if price["currency"] != cfg["search"]["currency"]:
            raise ValueError(f"currency {price['currency']}, expected {cfg['search']['currency']}")
        if it.get("requires_self_transfer") and not cfg["search"]["allow_self_transfer"]:
            continue
        s0 = segs[0]
        cc = s0["marketing_carrier_code"] or ""
        num = str(s0["flight_number"] or "").replace(" ", "").upper()
        offers.append({
            "price_inr": round(price["amount"]), "price_status": price["status"], "airline": cc,
            "flight_no": num if num.startswith(cc) else cc + num,
            "dep_local": s0["departure_time_local"], "stops": len(segs) - 1,
            "bags": (it.get("bags") or {}).get("checked")})
    return offers, bool(data.get("cache_hit"))


def ignav_search(cfg, origin, dest, day, max_stops, min_bags):
    """→ (offers, cache_hit, attempts). Raises ProviderError."""
    if os.environ.get("DRY_RUN") == "1":
        data, attempts = _fixture(origin, dest), 1
    else:
        data, attempts = post(cfg, body(cfg, origin, dest, day, max_stops, min_bags))
    try:
        offers, cache_hit = parse(data, cfg)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        raise ProviderError(f"malformed response: {e!r}", None, attempts) from None
    return offers, cache_hit, attempts


def _fixture(origin, dest):
    """DRY_RUN: a recorded response (fixtures/<O>_<D>.json, else sample) with prices jittered ±10%."""
    f = FIXTURES / f"{origin}_{dest}.json"
    data = json.loads((f if f.exists() else FIXTURES / "sample_oneway.json").read_text())
    for it in data["itineraries"]:
        it["price"]["amount"] *= random.uniform(0.9, 1.1)
    return data


def cheapest(offers, route, max_stops):
    """Cheapest offer passing the route's airline / flight / stops / time-of-day / bags filters."""
    def ok(o):
        m = int(o["dep_local"][11:13]) * 60 + int(o["dep_local"][14:16])
        return ((not route["airline"] or o["airline"] == route["airline"])
                and (not route["flight_no"] or o["flight_no"] == route["flight_no"])
                and o["stops"] <= max_stops
                and (route["time_start"] is None or route["time_start"] * 60 <= m <= route["time_end"] * 60)
                # unknown allowance passes: Ignav already filtered server-side (min_checked_bags), and
                # 6E/QP return bags=None
                and (not route["min_bags"] or o["bags"] is None or o["bags"] >= route["min_bags"]))
    return min(filter(ok, offers), key=lambda o: o["price_inr"], default=None)
