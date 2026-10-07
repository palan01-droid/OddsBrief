import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
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
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # other sites can't post forms as a logged-in user
ADMIN = os.environ.get("ADMIN_USERNAME", "").lower()

REFRESH_SECONDS = 600
STATE_FILE = "state.json"

# start from the last run's page so it shows up right away
try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except FileNotFoundError:
    state = {}


# ---------- data ----------

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
        return ai.ask(BRIEF_PROMPT.format(moves=moves, bets=bets_text))
    except Exception as e:
        return f"Couldn't write the brief right now. ({e})"


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
        "theme": theme.pick_theme(movers),
        "brief": state.get("brief", "Writing today's brief..."),
    })

    # the brief is slow, so the page goes up first and the brief fills in after
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


# ---------- users ----------

hits = defaultdict(list)


def too_many(key, limit, seconds):
    now = time.time()
    hits[key] = [t for t in hits[key] if now - t < seconds]
    if len(hits[key]) >= limit:
        return True
    hits[key].append(now)
    return False


def get_db():
    return sqlite3.connect(collect.DB)


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    return {"id": row[0], "username": row[1], "admin": row[1].lower() == ADMIN} if row else None


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
            cur = db.execute("INSERT INTO users (username, password_hash, created) VALUES (?,?,?)",
                             (username, generate_password_hash(password), datetime.now(timezone.utc).isoformat()))
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
        except sqlite3.IntegrityError:
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
    if db.execute("DELETE FROM likes WHERE user_id=? AND ticker=?", (user["id"], ticker)).rowcount == 0:
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
        cur = db.execute("INSERT INTO comments (user_id, ticker, text, time) VALUES (?,?,?,?)",
                         (user["id"], ticker, text, datetime.now(timezone.utc).isoformat()))
        db.commit()
        db.close()
        market = next((f"{m['event']} - {m['pick']}" for m in state.get("movers", []) if m["ticker"] == ticker), ticker)
        rag.add_comment(cur.lastrowid, user["username"], market, text)
    return redirect(url_for("home") + "#" + ticker)


@app.route("/comment/<int:comment_id>/delete", methods=["POST"])
def delete_comment(comment_id):
    user = current_user()
    if user:
        db = get_db()
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
    db = get_db()
    marks = ",".join("?" * len(tickers))
    likes = dict(db.execute(f"SELECT ticker, COUNT(*) FROM likes WHERE ticker IN ({marks}) GROUP BY ticker", tickers))
    mine = {t for (t,) in db.execute("SELECT ticker FROM likes WHERE user_id=?", (user["id"] if user else -1,))}
    comments = defaultdict(list)
    for cid, ticker, text, when, uid, name in db.execute(f"""
        SELECT c.id, c.ticker, c.text, c.time, u.id, u.username FROM comments c JOIN users u ON u.id = c.user_id
        WHERE c.ticker IN ({marks}) ORDER BY c.time
    """, tickers):
        can_delete = bool(user) and (user["admin"] or user["id"] == uid)
        comments[ticker].append({"id": cid, "text": text, "time": when[:16].replace("T", " "), "user": name, "can_delete": can_delete})
    db.close()
    return likes, mine, comments


# ---------- chatbot ----------

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
    lines = [f"Last update: {state.get('updated', 'not yet')}"]
    for m in state.get("movers", []):
        lines.append(f"Mover: {m['event']} / {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}")
    for e in state.get("edges", []):
        lines.append(f"Model disagrees: {e['event']} / {e['pick']}: market {e['price']:.0%}, model {e['model']:.0%}")
    score = state.get("score") or {}
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
    if not message:
        return jsonify({"reply": "Ask me something about OddsBrief."})
    history = data.get("history", [])[-6:]
    transcript = "\n".join(f"{'User' if h.get('role') == 'user' else 'Bot'}: {str(h.get('text', ''))[:500]}" for h in history)
    prompt = f"{transcript}\nUser: {message}\nBot:"

    hits = rag.search(message)
    sources = "\n\n".join(f"[{i + 1}] {h['text']}" for i, h in enumerate(hits)) or "none"
    try:
        reply = ai.ask(prompt, system=CHAT_SYSTEM.format(context=chat_context(), sources=sources))
    except Exception as e:
        print("chat failed:", e)
        return jsonify({"reply": "Sorry, I can't answer right now.", "sources": []})
    # only show the sources the answer actually used
    used = [{"n": i + 1, "type": h["type"], "title": h["title"]} for i, h in enumerate(hits) if f"[{i + 1}]" in reply]
    return jsonify({"reply": reply, "sources": used})


# ---------- pages ----------

@app.route("/")
def home():
    user = current_user()
    if not user and "guest" not in session:
        return render_template("welcome.html")
    movers = state.get("movers", [])
    likes, mine, comments = social([m["ticker"] for m in movers] or [""], user)
    page = dict(state)
    page["theme"] = page.get("theme") or dict(theme.DEFAULT, emojis=[])
    return render_template("index.html", refresh=REFRESH_SECONDS, user=user, likes=likes, mine=mine,
                           comments=comments, **page)


@app.route("/live")
def live_data():
    tickers = request.args.get("tickers", "").split(",")
    db = get_db()
    bets = big_bets(db)
    db.close()
    return jsonify({"prices": {t: live.prices[t] for t in tickers if t in live.prices}, "bets": bets,
                    "updated": state.get("updated"), "brief": state.get("brief")})


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    threading.Thread(target=live.run_forever, daemon=True).start()
    threading.Thread(target=refresh_forever, daemon=True).start()
    app.run(port=5050)
