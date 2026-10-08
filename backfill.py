import sys

from collect import db, get_events, save_market, fill_forecasts

# run: python backfill.py 30   (more pages = more past markets to learn from)
PAGES = 5
if len(sys.argv) > 1:
    PAGES = int(sys.argv[1])
MIN_VOLUME = 10000

count = 0
for e, m in get_events("settled", max_pages=PAGES):
    if m["result"] in ("yes", "no") and float(m["volume_fp"] or 0) >= MIN_VOLUME:
        save_market(e, m, result=m["result"])
        count += 1
db.commit()
print(f"Saved {count} settled markets, getting their price history...")

fill_forecasts()
n = db.execute("SELECT COUNT(*) FROM markets WHERE forecast >= 0").fetchone()[0]
print(f"Done. {n} markets ready to learn from")
