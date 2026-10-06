import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template_string, request

import collect
import learn
import live

app = Flask(__name__)
REFRESH_SECONDS = 600
STATE_FILE = "state.json"

# start from the last run's page so it shows up right away
try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except FileNotFoundError:
    state = {}


def sparkline(series, ticker):
    w, h = 140, 36
    now = int(time.time())
    prices = [p for ts, p in collect.candles(series, ticker, now - 2 * 86400, now)]
    if len(prices) < 2:
        return ""
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1
    step = w / (len(prices) - 1)
    return " ".join(f"{i * step:.1f},{h - (p - lo) / span * h:.1f}" for i, p in enumerate(prices))


def big_bets(db):
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = db.execute("""
        SELECT title, side, price, dollars, time FROM trades
        WHERE time > ? ORDER BY dollars DESC LIMIT 10
    """, (since,)).fetchall()
    return [{"title": r[0], "side": r[1], "price": r[2], "dollars": r[3], "time": r[4][11:16]} for r in rows]


BRIEF_PROMPT = """You write a short daily brief about prediction markets on Kalshi.
Prices are the market's odds that something happens. Only use the numbers below.
Don't guess why things moved and don't make anything up. Write 3-4 plain sentences, no lists. Start directly with the first sentence.

Biggest moves in the last 24 hours:
{moves}

Biggest bets today:
{bets}

Brief:"""


def write_brief(movers, bets):
    moves = "\n".join(f"- {m['event']}, {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}" for m in movers)
    bets_text = "\n".join(
        f"- Someone bet ${b['dollars']:,.0f} that this {'WILL' if b['side'] == 'yes' else 'will NOT'} happen: {b['title']}"
        for b in bets[:5]
    ) or "- none yet"
    try:
        r = requests.post("http://localhost:11434/api/generate", timeout=120, json={
            "model": "llama3.2",
            "prompt": BRIEF_PROMPT.format(moves=moves, bets=bets_text),
            "stream": False,
            "options": {"temperature": 0},
        })
        r.raise_for_status()
        return r.json()["response"].strip()
    except Exception as e:
        return f"Couldn't write the brief, is Ollama running? ({e})"


def refresh():
    markets = collect.scan()
    collect.resolve()

    db = sqlite3.connect(collect.DB)
    cal, score = learn.train(db)

    # only markets that are still undecided are interesting
    in_play = [m for m in markets if m["prev"] > 0 and 0.05 < m["price"] < 0.95]
    movers = sorted(in_play, key=lambda m: abs(m["price"] - m["prev"]), reverse=True)[:8]
    for m in movers:
        m["spark"] = sparkline(m["series"], m["ticker"])
        m["depth"] = collect.orderbook_depth(m["ticker"])

    edges = []
    if score["n"] >= 50:
        for m in markets:
            if 0.05 < m["price"] < 0.95:
                m["model"] = cal.predict(m["price"], m["category"])
                if abs(m["model"] - m["price"]) >= 0.03:
                    edges.append(m)
        edges.sort(key=lambda m: abs(m["model"] - m["price"]), reverse=True)

    bets = big_bets(db)
    db.close()

    state.update({
        "updated": datetime.now().strftime("%-I:%M %p"),
        "movers": movers,
        "edges": edges[:6],
        "score": score,
        "table": learn.calibration_table(cal),
        "brief": state.get("brief", "Writing today's brief..."),
    })

    # the brief is slow, so the page goes up first and the brief fills in after
    state["brief"] = write_brief(movers, bets)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def refresh_forever():
    while True:
        try:
            refresh()
            print("refreshed at", state["updated"])
        except Exception as e:
            print("refresh failed:", e)
        time.sleep(REFRESH_SECONDS)


PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OddsBrief</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 900px; margin: 30px auto; padding: 0 16px; color: #222; }
        h1 { margin-bottom: 4px; }
        h2 { margin-top: 36px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
        .gray { color: #777; font-size: 14px; }
        .brief { background: #f5f5f5; padding: 14px; border-radius: 6px; line-height: 1.5; }
        .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
        .card { border: 1px solid #ddd; border-radius: 6px; padding: 12px; }
        .tag { font-size: 12px; color: #555; background: #eee; padding: 2px 6px; border-radius: 4px; }
        .price { font-size: 26px; font-weight: bold; }
        .up { color: green; } .down { color: #c00; }
        .flash { background: #fff3b0; transition: background 1s; }
        table { border-collapse: collapse; width: 100%; }
        td, th { text-align: left; padding: 6px; border-bottom: 1px solid #eee; }
        polyline { fill: none; stroke-width: 2; }
    </style>
</head>
<body>
<h1>OddsBrief</h1>
<div class="gray">Kalshi prediction markets. Prices update live, everything else every {{ refresh // 60 }} min.
    {% if updated %}Last full update {{ updated }}.{% endif %} Not financial advice.</div>

{% if not updated %}
<p>Loading the first scan, this takes about a minute. The page will reload by itself.</p>
<script>setTimeout(() => location.reload(), 15000)</script>
{% else %}

<h2>Today's brief</h2>
<div class="brief" id="brief">{{ brief }}</div>

<h2>Biggest moves (24h)</h2>
<div class="cards">
{% for m in movers %}
    <div class="card">
        <span class="tag">{{ m.tag or m.category }}</span>
        <p><b>{{ m.event }}</b><br>{{ m.pick }}</p>
        <span class="price" data-ticker="{{ m.ticker }}">{{ '%.0f' % (m.price * 100) }}%</span>
        <span class="{{ 'up' if m.price > m.prev else 'down' }}">
            {{ '%+.0f' % ((m.price - m.prev) * 100) }} pts</span>
        {% if m.spark %}
        <br><svg width="140" height="40" viewBox="-2 -2 144 40">
            <polyline points="{{ m.spark }}" stroke="{{ 'green' if m.price > m.prev else '#c00' }}"/></svg>
        {% endif %}
        <div class="gray">Order book: ${{ '{:,.0f}'.format(m.depth.yes) }} waiting on YES,
            ${{ '{:,.0f}'.format(m.depth.no) }} on NO</div>
    </div>
{% endfor %}
</div>

<h2>Big bets (last 24h)</h2>
<table id="bets"><tr><td class="gray">Waiting for big bets...</td></tr></table>

<h2>Where my model disagrees with the market</h2>
{% if edges %}
<table>
    <tr><th>Market</th><th>Market says</th><th>Model says</th></tr>
    {% for e in edges %}
    <tr>
        <td>{{ e.event }} - {{ e.pick }}</td>
        <td data-ticker="{{ e.ticker }}">{{ '%.0f' % (e.price * 100) }}%</td>
        <td class="{{ 'up' if e.model > e.price else 'down' }}">{{ '%.0f' % (e.model * 100) }}%</td>
    </tr>
    {% endfor %}
</table>
{% else %}
<p class="gray">Not enough resolved markets yet ({{ score.n }}), needs 50.</p>
{% endif %}

<h2>How the model is doing</h2>
<p class="gray">Every market is scored when it settles (Brier score, lower is better, 0.25 = coin flip).
    The model is scored before it learns from each result.</p>
<p>Resolved markets: <b>{{ score.n }}</b>
    {% if score.n %} | Market: <b>{{ '%.4f' % score.market }}</b> | Model: <b>{{ '%.4f' % score.model }}</b>{% endif %}</p>
<table>
    <tr><th>When the market said</th><th>It actually happened</th><th>Markets</th></tr>
    {% for label, rate, n in table %}
    <tr><td>{{ label }}</td><td>{{ '%.0f' % (rate * 100) }}%</td><td>{{ n }}</td></tr>
    {% endfor %}
</table>

<script>
function esc(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

async function updateLive() {
    const tickers = [...new Set([...document.querySelectorAll("[data-ticker]")].map(el => el.dataset.ticker))];
    const res = await fetch("/live?tickers=" + tickers.join(","));
    const data = await res.json();

    // new data is ready, reload to show it
    if (data.updated !== "{{ updated }}") location.reload();
    document.getElementById("brief").textContent = data.brief;

    document.querySelectorAll("[data-ticker]").forEach(el => {
        const p = data.prices[el.dataset.ticker];
        if (p === undefined) return;
        const text = Math.round(p * 100) + "%";
        if (el.textContent !== text) {
            el.textContent = text;
            el.classList.add("flash");
            setTimeout(() => el.classList.remove("flash"), 1000);
        }
    });

    const rows = data.bets.map(b =>
        `<tr><td>${b.time} UTC</td><td><b>$${Math.round(b.dollars).toLocaleString()}</b> on ${b.side.toUpperCase()}
         at ${Math.round(b.price * 100)}¢</td><td>${esc(b.title)}</td></tr>`);
    if (rows.length) document.getElementById("bets").innerHTML = rows.join("");
}
updateLive();
setInterval(updateLive, 3000);
</script>
{% endif %}
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(PAGE, refresh=REFRESH_SECONDS, **state)


@app.route("/live")
def live_data():
    tickers = request.args.get("tickers", "").split(",")
    db = sqlite3.connect(collect.DB)
    bets = big_bets(db)
    db.close()
    return jsonify({"prices": {t: live.prices[t] for t in tickers if t in live.prices}, "bets": bets,
                    "updated": state.get("updated"), "brief": state.get("brief")})


if __name__ == "__main__":
    threading.Thread(target=live.run_forever, daemon=True).start()
    threading.Thread(target=refresh_forever, daemon=True).start()
    app.run(port=5050)
