"""Phase 1: live Ignav checks that also save real responses as test fixtures.

    python validate.py              # HYD → DEL/BOM/BLR/GOI/CCU, 30 days out (5 calls)
    python validate.py --cache-test # HYD→DEL same date at 0/15/30/60 min (4 calls, ~1 h)

Calls are recorded in the bot's spend ledger so /status shows the right free-call count."""
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta

import budget
import config
import db
import providers

CARRIERS = {"6E", "AI", "IX", "QP", "SG"}


def call(cfg, dest, day):
    try:
        raw, attempts = providers.post(cfg, providers.body(cfg, "HYD", dest, day, cfg["search"]["max_stops"]))
    except providers.ProviderError as e:
        budget.record(cfg, datetime.now(UTC), "validate", e.attempts)
        sys.exit(f"HYD→{dest}: {e.message}")
    budget.record(cfg, datetime.now(UTC), "validate", attempts)
    return raw


def main():
    config.load_env()
    cfg = config.load()
    db.init()
    day = (date.today() + timedelta(days=30)).isoformat()
    if "--cache-test" in sys.argv:
        for i, wait in enumerate([0, 15, 15, 30]):
            time.sleep(wait * 60)
            raw = call(cfg, "DEL", day)
            (providers.FIXTURES / f"HYD_DEL_cache{i}.json").write_text(json.dumps(raw, indent=1))
            offers, hit = providers.parse(raw, cfg)
            print(f"t+{[0, 15, 30, 60][i]:>2} min cache_hit={hit} observed_at={raw.get('observed_at')} "
                  f"cheapest=₹{min(o['price_inr'] for o in offers) if offers else '-'}", flush=True)
        return
    seen = set()
    for dest in ["DEL", "BOM", "BLR", "GOI", "CCU"]:
        raw = call(cfg, dest, day)
        (providers.FIXTURES / f"HYD_{dest}.json").write_text(json.dumps(raw, indent=1))
        offers, hit = providers.parse(raw, cfg)
        carriers = {o["airline"] for o in offers}
        seen |= carriers
        cheapest = min(offers, key=lambda o: o["price_inr"], default=None)
        print(f"HYD→{dest} {day}: {len(offers)} itineraries, carriers {sorted(carriers)}, cache_hit={hit}, "
              f"cheapest {cheapest and (cheapest['price_inr'], cheapest['flight_no'], cheapest['dep_local'])}")
    print(f"Missing carriers: {sorted(CARRIERS - seen) or 'none'}. "
          "Now hand-check 5 of these prices on the airline sites (±3%).")


if __name__ == "__main__":
    main()
