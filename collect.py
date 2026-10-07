import os
import sqlite3
import time
from datetime import datetime

import requests

API = "https://external-api.kalshi.com/trade-api/v2"
DB = "oddsbrief.db"
TOP_N = 500
BIG_BET = 5000

# load settings from .env (on the server these are already environment variables)
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                name, value = line.split("=", 1)
                if name not in os.environ:
                    os.environ[name] = value

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
    # kalshi sends 429 if we go too fast, so wait and try again
    wait = 1
    for attempt in range(4):
        r = requests.get(API + path, params=params, timeout=30)
        if r.status_code != 429:
            break
        time.sleep(wait)
        wait = wait * 2
    r.raise_for_status()
    return r.json()


def to_timestamp(iso):
    iso = iso.replace("Z", "+00:00")
    return int(datetime.fromisoformat(iso).timestamp())


def price_of(m):
    bid = float(m["yes_bid_dollars"])
    ask = float(m["yes_ask_dollars"])
    # if the bid and ask are close, the middle is a better price than the last trade
    if bid > 0 and ask > 0 and ask - bid <= 0.10:
        return round((bid + ask) / 2, 4)
    return float(m["last_price_dollars"])


def get_events(status, max_pages=None):
    # returns a list of (event, market) pairs, going through every page
    results = []
    cursor = None
    pages = 0
    while True:
        page = get("/events", limit=200, status=status, with_nested_markets="true", cursor=cursor)
        for event in page["events"]:
            markets = event.get("markets")
            if markets is None:
                continue
            for market in markets:
                results.append((event, market))
        cursor = page.get("cursor")
        pages += 1
        if not cursor or pages == max_pages:
            break
    return results


def get_tags():
    # series ticker -> a short label like "Football"
    tags = {}
    for s in get("/series")["series"]:
        if s.get("tags"):
            tags[s["ticker"]] = s["tags"][0]
        elif s.get("category"):
            tags[s["ticker"]] = s["category"]
        else:
            tags[s["ticker"]] = ""
    return tags


def save_market(e, m, tags, result=None):
    subtitle = m.get("yes_sub_title") or m["title"]
    category = e.get("category") or "Other"
    tag = tags.get(e["series_ticker"], "")
    db.execute("INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?,?,NULL)",
               (m["ticker"], e["series_ticker"], e["title"], subtitle, category, tag, m["close_time"], result))


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
    # what the market said a day before it closed
    # (or its first price if it wasn't open that long)
    end = to_timestamp(close_time)
    prices = candles(series, ticker, end - 3 * 86400, end)
    if len(prices) == 0:
        return None
    forecast = prices[0][1]
    for ts, price in prices:
        if ts <= end - 86400:
            forecast = price
    return forecast


def fill_forecasts():
    todo = db.execute(
        "SELECT ticker, series, close_time FROM markets WHERE result IS NOT NULL AND forecast IS NULL"
    ).fetchall()
    for ticker, series, close_time in todo:
        forecast = forecast_for(series, ticker, close_time)
        if forecast is None:
            forecast = -1  # no price history, learn.py skips these
        db.execute("UPDATE markets SET forecast=? WHERE ticker=?", (forecast, ticker))
    db.commit()


def resolve():
    # check which of our markets have finished
    pending = []
    for row in db.execute("SELECT ticker FROM markets WHERE result IS NULL"):
        pending.append(row[0])
    # kalshi lets you ask about 100 tickers at once
    for i in range(0, len(pending), 100):
        batch = ",".join(pending[i:i + 100])
        for m in get("/markets", tickers=batch)["markets"]:
            if m["result"] in ("yes", "no"):
                db.execute("UPDATE markets SET result=?, close_time=? WHERE ticker=?",
                           (m["result"], m["close_time"], m["ticker"]))
    db.commit()
    fill_forecasts()


def orderbook_depth(ticker):
    # dollars waiting to buy YES and to buy NO, within 5 cents of the best price
    book = get(f"/markets/{ticker}/orderbook")["orderbook_fp"]
    depth = {}
    for side in ["yes", "no"]:
        levels = book.get(side + "_dollars") or []
        best = 0
        for price, qty in levels:
            best = max(best, float(price))
        total = 0
        for price, qty in levels:
            if float(price) >= best - 0.05:
                total += float(price) * float(qty)
        depth[side] = total
    return depth


def volume_24h(row):
    event, market = row
    return float(market["volume_24h_fp"] or 0)


def scan():
    # get every open market and keep the most traded ones
    rows = get_events("open")
    rows.sort(key=volume_24h, reverse=True)
    rows = rows[:TOP_N]
    tags = get_tags()

    markets = []
    for e, m in rows:
        save_market(e, m, tags)
        rules = (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or "")
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
            "rules": rules.strip(),
        })
    db.commit()
    return markets


if __name__ == "__main__":
    markets = scan()
    resolve()
    print("tracking", len(markets), "markets")
