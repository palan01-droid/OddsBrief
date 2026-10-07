import asyncio
import base64
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from functools import lru_cache

import websockets
from cryptography.hazmat.primitives import serialization

from collect import DB, BIG_BET, get, price_of

WS_HOST = "wss://external-api-ws.kalshi.com"
WS_PATH = "/trade-api/ws/v2"

prices = {}


def auth_headers():
    # the key can be a file path (local) or the key text itself (on a host)
    if os.environ.get("KALSHI_PRIVATE_KEY"):
        pem = os.environ["KALSHI_PRIVATE_KEY"].replace("\\n", "\n").encode()
    else:
        pem = open(os.environ["KALSHI_KEY_PATH"], "rb").read()
    key = serialization.load_pem_private_key(pem, password=None)
    ts = str(int(time.time() * 1000))
    signature = key.sign((ts + "GET" + WS_PATH).encode())
    return {
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


@lru_cache
def title_for(ticker):
    m = get(f"/markets/{ticker}")["market"]
    sub = m.get("yes_sub_title") or ""
    return m["title"] + (f" ({sub})" if sub and sub not in m["title"] else "")


def save_trade(db, t):
    side = t["taker_side"]
    price = float(t[f"{side}_price_dollars"])
    dollars = float(t["count_fp"]) * price
    # skip parlays (KXMVE...) and "bets" on things that are already basically decided
    if dollars < BIG_BET or t["market_ticker"].startswith("KXMVE") or price >= 0.97:
        return
    when = datetime.fromtimestamp(t["ts"], timezone.utc).isoformat()
    db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?)",
               (t["trade_id"], t["market_ticker"], title_for(t["market_ticker"]), when, side, price, round(dollars, 2)))
    db.commit()


async def listen():
    db = sqlite3.connect(DB)
    async with websockets.connect(WS_HOST + WS_PATH, additional_headers=auth_headers()) as ws:
        # no market list = every market on Kalshi
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker", "trade"]}}))
        print("live: connected")
        async for raw in ws:
            msg = json.loads(raw)
            data = msg.get("msg", {})
            if msg["type"] == "ticker":
                data["last_price_dollars"] = data["price_dollars"]
                prices[data["market_ticker"]] = price_of(data)
            elif msg["type"] == "trade":
                save_trade(db, data)


def run_forever():
    while True:
        try:
            asyncio.run(listen())
        except Exception as e:
            print("live: disconnected, retrying in 5s:", e)
        time.sleep(5)
