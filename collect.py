import os
import sqlite3
import time
from datetime import datetime

import requests

API = "https://external-api.kalshi.com/trade-api/v2"
DB = "oddsbrief.db"
TOP_N = 500
BIG_BET = 5000

# locally settings come from .env, on a host they're already set as environment variables
if os.path.exists(".env"):
    for line in open(".env"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v)

db = sqlite3.connect(DB, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY, series TEXT, event_title TEXT, subtitle TEXT,
    category TEXT, tag TEXT, close_time TEXT, result TEXT, forecast REAL
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY, ticker TEXT, title TEXT, time TEXT, side TEXT, price REAL, dollars REAL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE, password_hash TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER, ticker TEXT, PRIMARY KEY (user_id, ticker)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY, user_id INTEGER, ticker TEXT, text TEXT, time TEXT
);
""")


def get(path, **params):
    for attempt in range(4):
        r = requests.get(API + path, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def to_ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def price_of(m):
    bid = float(m["yes_bid_dollars"])
    ask = float(m["yes_ask_dollars"])
    if bid > 0 and ask > 0 and ask - bid <= 0.10:
        return round((bid + ask) / 2, 4)
    return float(m["last_price_dollars"])


def get_events(status, max_pages=None):
    cursor = None
    pages = 0
    while True:
        page = get("/events", limit=200, status=status, with_nested_markets="true", cursor=cursor)
        for e in page["events"]:
            for m in e.get("markets") or []:
                yield e, m
        cursor = page.get("cursor")
        pages += 1
        if not cursor or pages == max_pages:
            return


def get_tags():
    tags = {}
    for s in get("/series")["series"]:
        tags[s["ticker"]] = (s.get("tags") or [s.get("category") or ""])[0]
    return tags


def save_market(e, m, tags, result=None):
    db.execute(
        "INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?,?,NULL)",
        (m["ticker"], e["series_ticker"], e["title"], m.get("yes_sub_title") or m["title"],
         e.get("category") or "Other", tags.get(e["series_ticker"], ""), m["close_time"], result),
    )


def candles(series, ticker, start, end):
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    rows = get(path, start_ts=start, end_ts=end, period_interval=60)["candlesticks"]
    prices = []
    for c in rows:
        close = c["price"].get("close_dollars")
        if close is None:
            bid = c["yes_bid"].get("close_dollars")
            ask = c["yes_ask"].get("close_dollars")
            if bid is None or ask is None:
                continue
            close = (float(bid) + float(ask)) / 2
        prices.append((c["end_period_ts"], float(close)))
    return prices


def forecast_for(series, ticker, close_time):
    # what the market said a day before it closed, or its first price if it wasn't open that long
    end = to_ts(close_time)
    prices = candles(series, ticker, end - 3 * 86400, end)
    if not prices:
        return None
    day_before = [p for ts, p in prices if ts <= end - 86400]
    return day_before[-1] if day_before else prices[0][1]


def fill_forecasts():
    todo = db.execute(
        "SELECT ticker, series, close_time FROM markets WHERE result IS NOT NULL AND forecast IS NULL"
    ).fetchall()
    for ticker, series, close_time in todo:
        f = forecast_for(series, ticker, close_time)
        # -1 means no price history, so we skip it when learning
        db.execute("UPDATE markets SET forecast=? WHERE ticker=?", (-1 if f is None else f, ticker))
    db.commit()


def resolve():
    pending = [t for (t,) in db.execute("SELECT ticker FROM markets WHERE result IS NULL")]
    for i in range(0, len(pending), 100):
        for m in get("/markets", tickers=",".join(pending[i:i + 100]))["markets"]:
            if m["result"] in ("yes", "no"):
                db.execute("UPDATE markets SET result=?, close_time=? WHERE ticker=?",
                           (m["result"], m["close_time"], m["ticker"]))
    db.commit()
    fill_forecasts()


def orderbook_depth(ticker):
    # dollars waiting to buy YES and to buy NO, within 5 cents of the best price
    book = get(f"/markets/{ticker}/orderbook")["orderbook_fp"]
    depth = {}
    for side in ("yes", "no"):
        levels = [(float(p), float(q)) for p, q in book.get(f"{side}_dollars") or []]
        best = max((p for p, q in levels), default=0)
        depth[side] = sum(p * q for p, q in levels if p >= best - 0.05)
    return depth


def scan():
    rows = list(get_events("open"))
    rows.sort(key=lambda r: float(r[1]["volume_24h_fp"] or 0), reverse=True)
    rows = rows[:TOP_N]
    tags = get_tags()

    markets = []
    for e, m in rows:
        save_market(e, m, tags)
        markets.append({
            "ticker": m["ticker"],
            "series": e["series_ticker"],
            "event": e["title"],
            "pick": m.get("yes_sub_title") or m["title"],
            "category": e.get("category") or "Other",
            "tag": tags.get(e["series_ticker"], ""),
            "price": price_of(m),
            "prev": float(m["previous_price_dollars"] or 0),
            "volume": float(m["volume_24h_fp"] or 0),
            "rules": " ".join(filter(None, [m.get("rules_primary"), m.get("rules_secondary")])),
        })
    db.commit()
    return markets


if __name__ == "__main__":
    markets = scan()
    resolve()
    print("tracking", len(markets), "markets")
