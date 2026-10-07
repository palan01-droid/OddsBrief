import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import collect
import learn
import live
import rag
import theme

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # so other websites can't submit forms as a logged in user
ADMIN = os.environ.get("ADMIN_USERNAME", "").lower()

REFRESH_SECONDS = 600
STATE_FILE = "state.json"

# load the page from last time so it shows up right away instead of waiting for a scan
try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except FileNotFoundError:
    state = {}


def sparkline(series, ticker):
    # turns the last 48 hours of prices into points for an svg line, like "0,20 4,18 8,25"
    width = 140
    height = 36
    now = int(time.time())
    prices = []
    for ts, price in collect.candles(series, ticker, now - 2 * 86400, now):
        prices.append(price)
    if len(prices) < 2:
        return ""

    low = min(prices)
    high = max(prices)
    span = high - low
    if span == 0:
        span = 1
    step = width / (len(prices) - 1)

    points = []
    for i in range(len(prices)):
        x = i * step
        y = height - (prices[i] - low) / span * height
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def big_bets(db):
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = db.execute("""
        SELECT title, side, price, dollars, time FROM trades
        WHERE time > ? ORDER BY dollars DESC LIMIT 10
    """, (since,)).fetchall()
    bets = []
    for title, side, price, dollars, when in rows:
        bets.append({"title": title, "side": side, "price": price, "dollars": dollars, "time": when[11:16]})
    return bets


BRIEF_PROMPT = """You write a short daily brief about prediction markets on Kalshi.
Prices are the market's odds that something happens. Only use the numbers below.
Don't guess why things moved and don't make anything up. Write 3-4 plain sentences, no lists. Start directly with the first sentence.

Biggest moves in the last 24 hours:
{moves}

Biggest bets today:
{bets}

Brief:"""


def write_brief(movers, bets):
    moves = ""
    for m in movers:
        moves += f"- {m['event']}, {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}\n"

    bets_text = ""
    for b in bets[:5]:
        if b["side"] == "yes":
            will = "WILL"
        else:
            will = "will NOT"
        bets_text += f"- Someone bet ${b['dollars']:,.0f} that this {will} happen: {b['title']}\n"
    if bets_text == "":
        bets_text = "- none yet"

    try:
        return ai.ask(BRIEF_PROMPT.format(moves=moves, bets=bets_text))
    except Exception as e:
        return f"Couldn't write the brief right now. ({e})"


def refresh():
    markets = collect.scan()
    collect.resolve()

    db = sqlite3.connect(collect.DB)
    cal, score = learn.train(db)

    # skip markets that are basically decided already (under 5% or over 95%)
    in_play = []
    for m in markets:
        if m["prev"] > 0 and 0.05 < m["price"] < 0.95:
            in_play.append(m)
    in_play.sort(key=lambda m: abs(m["price"] - m["prev"]), reverse=True)
    movers = in_play[:8]
    for m in movers:
        m["spark"] = sparkline(m["series"], m["ticker"])
        m["depth"] = collect.orderbook_depth(m["ticker"])

    # markets where the model's guess is at least 3 points away from the market
    edges = []
    if score["n"] >= 50:  # don't trust the model until it has seen some results
        for m in markets:
            if 0.05 < m["price"] < 0.95:
                m["model"] = cal.predict(m["price"], m["category"])
                if abs(m["model"] - m["price"]) >= 0.03:
                    edges.append(m)
        edges.sort(key=lambda m: abs(m["model"] - m["price"]), reverse=True)

    bets = big_bets(db)
    db.close()

    state["updated"] = datetime.now().strftime("%-I:%M %p")
    state["movers"] = movers
    state["edges"] = edges[:6]
    state["score"] = score
    state["table"] = learn.calibration_table(cal)
    state["theme"] = theme.pick_theme(movers)
    if "brief" not in state:
        state["brief"] = "Writing today's brief..."

    # writing the brief takes a few seconds, so the page updates first and the brief shows up after
    state["brief"] = write_brief(movers, bets)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    # save new market rules and this brief for the chatbot to search later
    try:
        added = rag.add_rules(markets)
        rag.add_brief(datetime.now().strftime("%b %-d, %-I:%M %p"), state["brief"], movers)
        print(f"rag: added {added} market rules, {rag.docs.count()} documents total")
    except Exception as e:
        print("rag update failed:", e)


def refresh_forever():
    while True:
        try:
            refresh()
            print("refreshed at", state["updated"])
        except Exception as e:
            print("refresh failed:", e)
        time.sleep(REFRESH_SECONDS)


# simple rate limiting: remember when each person did something
hits = {}


def too_many(key, limit, seconds):
    now = time.time()
    recent = []
    for t in hits.get(key, []):
        if now - t < seconds:
            recent.append(t)
    if len(recent) >= limit:
        hits[key] = recent
        return True
    recent.append(now)
    hits[key] = recent
    return False


def get_db():
    return sqlite3.connect(collect.DB)


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    if row is None:
        return None
    return {"id": row[0], "username": row[1], "admin": row[1].lower() == ADMIN}


@app.route("/signup", methods=["POST"])
def signup():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        flash("Username must be 3-20 letters, numbers or _.")
    elif not 8 <= len(password) <= 128:
        flash("Password must be at least 8 characters.")
    else:
        db = get_db()
        try:
            # never save the real password, only a hash of it
            password_hash = generate_password_hash(password)
            created = datetime.now(timezone.utc).isoformat()
            cur = db.execute("INSERT INTO users (username, password_hash, created) VALUES (?,?,?)",
                             (username, password_hash, created))
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
        except sqlite3.IntegrityError:  # username is UNIQUE in the table
            flash("That username is taken.")
        db.close()
    return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    if too_many(f"login:{request.remote_addr}", 10, 600):
        flash("Too many tries, wait a few minutes.")
        return redirect(url_for("home"))
    db = get_db()
    row = db.execute("SELECT id, password_hash FROM users WHERE username=?", (username,)).fetchone()
    db.close()
    if row and check_password_hash(row[1], request.form.get("password", "")):
        session.clear()
        session["user_id"] = row[0]
    else:
        flash("Wrong username or password.")
    return redirect(url_for("home"))


@app.route("/guest", methods=["POST"])
def guest():
    session.clear()
    session["guest"] = secrets.token_hex(8)
    return redirect(url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/like/<ticker>", methods=["POST"])
def like(ticker):
    user = current_user()
    if not user:
        flash("Make an account to like markets.")
        return redirect(url_for("home"))
    db = get_db()
    # clicking like again removes the like
    removed = db.execute("DELETE FROM likes WHERE user_id=? AND ticker=?", (user["id"], ticker)).rowcount
    if removed == 0:
        db.execute("INSERT INTO likes VALUES (?,?)", (user["id"], ticker))
    db.commit()
    db.close()
    return redirect(url_for("home") + "#" + ticker)


@app.route("/comment/<ticker>", methods=["POST"])
def comment(ticker):
    user = current_user()
    text = request.form.get("text", "").strip()
    if not user:
        flash("Make an account to comment.")
    elif not 1 <= len(text) <= 500:
        flash("Comments need to be 1-500 characters.")
    elif too_many(f"comment:{user['id']}", 5, 60):
        flash("Slow down a little.")
    else:
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        cur = db.execute("INSERT INTO comments (user_id, ticker, text, time) VALUES (?,?,?,?)",
                         (user["id"], ticker, text, now))
        db.commit()
        db.close()

        # also save it for the chatbot to search
        market = ticker
        for m in state.get("movers", []):
            if m["ticker"] == ticker:
                market = m["event"] + " - " + m["pick"]
        rag.add_comment(cur.lastrowid, user["username"], market, text)
    return redirect(url_for("home") + "#" + ticker)


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    user = current_user()
    if user:
        db = get_db()
        # admins can delete any comment, everyone else only their own
        if user["admin"]:
            deleted = db.execute("DELETE FROM comments WHERE id=?", (comment_id,)).rowcount
        else:
            deleted = db.execute("DELETE FROM comments WHERE id=? AND user_id=?", (comment_id, user["id"])).rowcount
        db.commit()
        db.close()
        if deleted:
            rag.delete_comment(comment_id)
    return redirect(request.referrer or url_for("home"))


def social(tickers, user):
    # like counts, which ones this user liked, and the comments, for the cards on the page
    db = get_db()
    marks = ",".join(["?"] * len(tickers))  # "?,?,?" one for each ticker

    likes = {}
    for ticker, count in db.execute(f"SELECT ticker, COUNT(*) FROM likes WHERE ticker IN ({marks}) GROUP BY ticker", tickers):
        likes[ticker] = count

    mine = []
    if user:
        for row in db.execute("SELECT ticker FROM likes WHERE user_id=?", (user["id"],)):
            mine.append(row[0])

    comments = {}
    for ticker in tickers:
        comments[ticker] = []
    rows = db.execute(f"""
        SELECT c.id, c.ticker, c.text, c.time, u.id, u.username FROM comments c JOIN users u ON u.id = c.user_id
        WHERE c.ticker IN ({marks}) ORDER BY c.time
    """, tickers)
    for comment_id, ticker, text, when, user_id, username in rows:
        can_delete = False
        if user and (user["admin"] or user["id"] == user_id):
            can_delete = True
        comments[ticker].append({"id": comment_id, "text": text, "time": when[:16].replace("T", " "),
                                 "user": username, "can_delete": can_delete})
    db.close()
    return likes, mine, comments


CHAT_SYSTEM = """You are the help bot for OddsBrief, a website that tracks Kalshi prediction markets.
Only answer questions about OddsBrief, the markets and numbers shown on it, prediction markets in general,
and how the site works. For anything else (homework, coding, other topics), say you can only help with OddsBrief.
Never give betting or financial advice, never tell people what to buy or sell. Keep answers short (2-4 sentences).
Only use the facts below and the numbered sources, if you don't know, say so.
When you use a numbered source, cite it like [1].

How OddsBrief works:
- It scans all open Kalshi markets every 10 minutes and tracks the 500 most traded.
- Prices are the market's odds: 70% means traders think there's about a 70% chance it happens.
- "Biggest moves" are the largest price changes in the last 24 hours. The line shows the last 48 hours.
- "Order book" is how much money is waiting to buy YES or NO near the current price.
- "Big bets" are single trades of $5,000 or more, streamed live from Kalshi.
- The model learns how often markets at each price really happen, by category, from past results.
  It's scored with a Brier score (lower is better) before it learns from each result.
- The daily brief, the colors and the emojis are picked by AI every refresh.
- Anyone can browse as a guest, an account is needed to like and comment.
- The "Ask the markets" box searches Kalshi's official market rules, past daily briefs and user comments.

What's on the site right now:
{context}

Sources found for this question (market rules, past briefs, user comments):
{sources}"""


def chat_context():
    # what's on the page right now, so the bot can talk about it
    lines = [f"Last update: {state.get('updated', 'not yet')}"]
    for m in state.get("movers", []):
        lines.append(f"Mover: {m['event']} / {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}")
    for e in state.get("edges", []):
        lines.append(f"Model disagrees: {e['event']} / {e['pick']}: market {e['price']:.0%}, model {e['model']:.0%}")
    score = state.get("score", {})
    if score.get("n"):
        lines.append(f"Model score: {score['n']} resolved markets, market Brier {score['market']:.4f}, model {score['model']:.4f}")
    db = get_db()
    for b in big_bets(db)[:5]:
        lines.append(f"Big bet: ${b['dollars']:,.0f} on {b['side'].upper()} at {b['price']:.0%} in {b['title']}")
    db.close()
    lines.append(f"Today's brief: {state.get('brief', '')}")
    return "\n".join(lines)


@app.route("/chat", methods=["POST"])
def chat():
    who = session.get("user_id") or session.get("guest")
    if not who:
        return jsonify({"reply": "Pick log in or guest first."}), 403
    if too_many(f"chat:{who}", 15, 600):
        return jsonify({"reply": "You've hit the chat limit, try again in a few minutes."}), 429

    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()[:500]
    if message == "":
        return jsonify({"reply": "Ask me something about OddsBrief."})

    # include the last few messages so follow up questions make sense
    prompt = ""
    for h in data.get("history", [])[-6:]:
        if h.get("role") == "user":
            prompt += "User: "
        else:
            prompt += "Bot: "
        prompt += str(h.get("text", ""))[:500] + "\n"
    prompt += "User: " + message + "\nBot:"

    # RAG: find the most relevant rules/briefs/comments and give them to the AI as numbered sources
    hits = rag.search(message)
    sources = ""
    for i in range(len(hits)):
        sources += f"[{i + 1}] {hits[i]['text']}\n\n"
    if sources == "":
        sources = "none"

    try:
        reply = ai.ask(prompt, system=CHAT_SYSTEM.format(context=chat_context(), sources=sources))
    except Exception as e:
        print("chat failed:", e)
        return jsonify({"reply": "Sorry, I can't answer right now.", "sources": []})

    # only send back the sources the answer actually cited
    used = []
    for i in range(len(hits)):
        if f"[{i + 1}]" in reply:
            used.append({"n": i + 1, "type": hits[i]["type"], "title": hits[i]["title"]})
    return jsonify({"reply": reply, "sources": used})


@app.route("/")
def home():
    user = current_user()
    if not user and "guest" not in session:
        return render_template("welcome.html")
    tickers = []
    for m in state.get("movers", []):
        tickers.append(m["ticker"])
    if len(tickers) == 0:
        tickers = [""]
    likes, mine, comments = social(tickers, user)

    return render_template("index.html", refresh=REFRESH_SECONDS, user=user, likes=likes, mine=mine,
                           comments=comments, updated=state.get("updated"), brief=state.get("brief"),
                           movers=state.get("movers", []), edges=state.get("edges", []),
                           score=state.get("score", {}), table=state.get("table", []),
                           theme=state.get("theme") or theme.default_theme(0))


@app.route("/live")
def live_data():
    # the page calls this every 3 seconds for new prices and bets
    prices = {}
    for ticker in request.args.get("tickers", "").split(","):
        if ticker in live.prices:
            prices[ticker] = live.prices[ticker]
    db = get_db()
    bets = big_bets(db)
    db.close()
    return jsonify({"prices": prices, "bets": bets, "updated": state.get("updated"), "brief": state.get("brief")})


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    # one thread listens to the websocket, one rescans every 10 minutes, flask serves the page
    threading.Thread(target=live.run_forever, daemon=True).start()
    threading.Thread(target=refresh_forever, daemon=True).start()
    app.run(port=5050)
