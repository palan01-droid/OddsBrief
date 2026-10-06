import sys

from collect import db, get_events, get_tags, save_market, fill_forecasts

PAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 5
MIN_VOLUME = 10000

tags = get_tags()
count = 0
for e, m in get_events("settled", max_pages=PAGES):
    if m["result"] in ("yes", "no") and float(m["volume_fp"] or 0) >= MIN_VOLUME:
        save_market(e, m, tags, result=m["result"])
        count += 1
db.commit()
print(f"Saved {count} settled markets, getting their price history...")

fill_forecasts()
n = db.execute("SELECT COUNT(*) FROM markets WHERE forecast >= 0").fetchone()[0]
print(f"Done. {n} markets ready to learn from")
