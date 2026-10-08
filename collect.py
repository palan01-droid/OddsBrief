import time
from datetime import datetime

import requests

import db

API = "https://external-api.kalshi.com/trade-api/v2"
TOP_N = 500
BIG_BET = 5000


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


def volume_24h(row):
    event, market = row
    return float(market["volume_24h_fp"] or 0)


def get_events(status, max_pages=None, keep_top=None):
    # returns a list of (event, market) pairs, going through every page.
    # keep_top only keeps the most traded ones as we go, so we don't hold 100k+ markets in memory
    results = []
    cursor = None
    pages = 0
    while True:
        # smaller pages use less memory (the free server only has 512 MB)
        page = get("/events", limit=100, status=status, with_nested_markets="true", cursor=cursor)
        for event in page["events"]:
            markets = event.get("markets")
            if markets is None:
                continue
            # only keep the event fields we use, so each market doesn't drag its whole event along
            info = {"title": event["title"], "series_ticker": event["series_ticker"], "category": event.get("category")}
            for market in markets:
                results.append((info, market))
        if keep_top and len(results) > keep_top * 4:
            results.sort(key=volume_24h, reverse=True)
            results = results[:keep_top]
        cursor = page.get("cursor")
        pages += 1
        if not cursor or pages == max_pages:
            break
    return results


tags = {}  # series ticker -> a short label like "Football", saved so we only look each one up once


def get_tag(series_ticker):
    # (the full /series list is ~20 MB, too big for the free server, so look them up one at a time)
    if series_ticker not in tags:
        s = get("/series/" + series_ticker)["series"]
        if s.get("tags"):
            tags[series_ticker] = s["tags"][0]
        elif s.get("category"):
            tags[series_ticker] = s["category"]
        else:
            tags[series_ticker] = ""
    return tags[series_ticker]


def market_row(e, m, result=None):
    # one row for the markets table
    subtitle = m.get("yes_sub_title") or m["title"]
    category = e.get("category") or "Other"
    tag = get_tag(e["series_ticker"])
    return (m["ticker"], e["series_ticker"], e["title"], subtitle, category, tag, m["close_time"], result)


def save_markets(rows):
    db.run_many("INSERT INTO markets VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT DO NOTHING", rows)


def candles(series, ticker, start, end, minutes=60):
    # minutes can be 1, 60 or 1440 (one candle per minute, hour or day)
    path = f"/series/{series}/markets/{ticker}/candlesticks"
    rows = get(path, start_ts=start, end_ts=end, period_interval=minutes)["candlesticks"]
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
    todo = db.query("SELECT ticker, series, close_time FROM markets WHERE result IS NOT NULL AND forecast IS NULL")
    updates = []
    for ticker, series, close_time in todo:
        forecast = forecast_for(series, ticker, close_time)
        if forecast is None:
            forecast = -1  # no price history, learn.py skips these
        updates.append((forecast, ticker))
    db.run_many("UPDATE markets SET forecast=%s WHERE ticker=%s", updates)


def resolve():
    # check which of our markets have finished
    pending = []
    for row in db.query("SELECT ticker FROM markets WHERE result IS NULL"):
        pending.append(row[0])
    # kalshi lets you ask about 100 tickers at once
    updates = []
    for i in range(0, len(pending), 100):
        batch = ",".join(pending[i:i + 100])
        for m in get("/markets", tickers=batch)["markets"]:
            if m["result"] in ("yes", "no"):
                updates.append((m["result"], m["close_time"], m["ticker"]))
    db.run_many("UPDATE markets SET result=%s, close_time=%s WHERE ticker=%s", updates)
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


def scan():
    # get every open market and keep the most traded ones
    rows = get_events("open", keep_top=TOP_N)
    rows.sort(key=volume_24h, reverse=True)
    rows = rows[:TOP_N]

    markets = []
    new_rows = []
    for e, m in rows:
        new_rows.append(market_row(e, m))
        rules = (m.get("rules_primary") or "") + " " + (m.get("rules_secondary") or "")
        markets.append({
            "ticker": m["ticker"],
            "series": e["series_ticker"],
            "event": e["title"],
            "pick": m.get("yes_sub_title") or m["title"],
            "category": e.get("category") or "Other",
            "tag": get_tag(e["series_ticker"]),
            "price": price_of(m),
            "prev": float(m["previous_price_dollars"] or 0),
            "volume": float(m["volume_24h_fp"] or 0),
            "rules": rules.strip(),
        })
    save_markets(new_rows)
    return markets


if __name__ == "__main__":
    markets = scan()
    resolve()
    print("tracking", len(markets), "markets")
