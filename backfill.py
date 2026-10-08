import sys

import db
from collect import fill_forecasts, get_events, market_row, save_markets

MIN_VOLUME = 10000


def backfill(pages=5):
    # save past markets that already finished, so the model has something to learn from
    rows = []
    for e, m in get_events("settled", max_pages=pages):
        if m["result"] in ("yes", "no") and float(m["volume_fp"] or 0) >= MIN_VOLUME:
            rows.append(market_row(e, m, result=m["result"]))
    save_markets(rows)
    print(f"backfill: saved {len(rows)} settled markets, getting their price history...")

    fill_forecasts()
    n = db.query("SELECT COUNT(*) FROM markets WHERE forecast >= 0")[0][0]
    print(f"backfill: done, {n} markets ready to learn from")


# run: python backfill.py 30   (more pages = more past markets to learn from)
if __name__ == "__main__":
    pages = 5
    if len(sys.argv) > 1:
        pages = int(sys.argv[1])
    backfill(pages)
