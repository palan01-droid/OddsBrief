import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import serialization

import db
from collect import BIG_BET, get, price_of

WS_HOST = "wss://external-api-ws.kalshi.com"
WS_PATH = "/trade-api/ws/v2"

prices = {}  # ticker -> latest price, filled in by the websocket
titles = {}  # cache so we only look up each market's name once


def auth_headers():
    # locally the key is a file, on the server the key text is an environment variable
    if os.environ.get("KALSHI_PRIVATE_KEY"):
        pem = os.environ["KALSHI_PRIVATE_KEY"].replace("\\n", "\n").encode()
    else:
        with open(os.environ["KALSHI_KEY_PATH"], "rb") as f:
            pem = f.read()
    key = serialization.load_pem_private_key(pem, password=None)

    # kalshi wants us to sign timestamp + method + path
    ts = str(int(time.time() * 1000))
    message = ts + "GET" + WS_PATH
    signature = key.sign(message.encode())
    return {
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


def title_for(ticker):
    if ticker in titles:
        return titles[ticker]
    m = get(f"/markets/{ticker}")["market"]
    title = m["title"]
    sub = m.get("yes_sub_title")
    if sub and sub not in title:
        title = title + " (" + sub + ")"
    titles[ticker] = title
    return title


def save_trade(t):
    side = t["taker_side"]
    price = float(t[side + "_price_dollars"])
    dollars = float(t["count_fp"]) * price
    if dollars < BIG_BET:
        return
    # skip parlays (their tickers start with KXMVE)
    if t["market_ticker"].startswith("KXMVE"):
        return
    # skip "bets" on things that are basically already decided
    if price >= 0.97:
        return
    when = datetime.fromtimestamp(t["ts"], timezone.utc).isoformat()
    title = title_for(t["market_ticker"])
    try:
        db.run("INSERT INTO trades VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
               (t["trade_id"], t["market_ticker"], title, when, side, price, round(dollars, 2)))
    except Exception as e:
        # a database hiccup shouldn't kill the websocket, just skip this one
        print("live: couldn't save trade:", type(e).__name__)


async def listen():
    async with websockets.connect(WS_HOST + WS_PATH, additional_headers=auth_headers()) as ws:
        # not giving a list of markets means we get every market on kalshi
        subscribe = {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker", "trade"]}}
        await ws.send(json.dumps(subscribe))
        print("live: connected")
        async for raw in ws:
            msg = json.loads(raw)
            data = msg.get("msg", {})
            if msg["type"] == "ticker":
                data["last_price_dollars"] = data["price_dollars"]
                prices[data["market_ticker"]] = price_of(data)
            elif msg["type"] == "trade":
                save_trade(data)


def run_forever():
    while True:
        try:
            asyncio.run(listen())
        except Exception as e:
            print("live: disconnected, retrying in 5s:", e)
        time.sleep(5)
