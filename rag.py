import os
import time

import requests

import db

# gemini turns text into 768 numbers (an "embedding"), texts that mean similar things get similar numbers.
# the numbers are saved in postgres with pgvector, which can find the closest ones fast
EMBED_MODEL = "gemini-embedding-2"
BATCH = 50              # texts per request
MAX_NEW_RULES = 150     # per refresh, the free tier only allows so many per minute


def embed(texts, task):
    # task is RETRIEVAL_DOCUMENT for things we save, RETRIEVAL_QUERY for questions
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None  # no key (running with ollama only), so no search
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + EMBED_MODEL + ":batchEmbedContents"
    vectors = []
    for i in range(0, len(texts), BATCH):
        if i > 0:
            time.sleep(15)  # spread big jobs out so we stay under the free tier's per-minute limit
        requests_list = []
        for text in texts[i:i + BATCH]:
            requests_list.append({"model": "models/" + EMBED_MODEL, "content": {"parts": [{"text": text}]},
                                  "taskType": task, "outputDimensionality": 768})
        # if gemini says "too many requests" (429), wait a bit and try again
        for attempt in range(3):
            r = requests.post(url, headers={"x-goog-api-key": key}, json={"requests": requests_list}, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(20 * (attempt + 1))
        r.raise_for_status()
        for e in r.json()["embeddings"]:
            vectors.append(e["values"])
    return vectors


def to_vector(values):
    # pgvector wants text like "[0.1,0.2,0.3]"
    return "[" + ",".join(str(v) for v in values) + "]"


def save(docs):
    # docs is a list of (id, type, title, text). saves new ones, replaces old ones with the same id
    vectors = embed([d[3] for d in docs], "RETRIEVAL_DOCUMENT")
    if not vectors:
        return
    rows = []
    for i in range(len(docs)):
        doc_id, doc_type, title, text = docs[i]
        rows.append((doc_id, doc_type, title, text, to_vector(vectors[i])))
    db.run_many("""
        INSERT INTO docs (id, type, title, text, embedding) VALUES (%s, %s, %s, %s, %s::vector)
        ON CONFLICT (id) DO UPDATE SET type = EXCLUDED.type, title = EXCLUDED.title,
                                       text = EXCLUDED.text, embedding = EXCLUDED.embedding
    """, rows)


def add_rules(markets):
    # only add markets we haven't saved yet (rules don't change)
    all_ids = []
    for m in markets:
        all_ids.append("rules:" + m["ticker"])
    already_saved = set()
    for row in db.query("SELECT id FROM docs WHERE id = ANY(%s)", (all_ids,)):
        already_saved.add(row[0])

    new_docs = []
    for m in markets:
        doc_id = "rules:" + m["ticker"]
        if doc_id in already_saved or not m.get("rules"):
            continue
        title = m["event"] + " - " + m["pick"]
        new_docs.append((doc_id, "rules", title, "Market rules for " + title + ":\n" + m["rules"]))
    # markets come sorted by volume, so the busiest ones get added first and the rest next time
    new_docs = new_docs[:MAX_NEW_RULES]
    if len(new_docs) > 0:
        save(new_docs)
    return len(new_docs)


def add_brief(when, brief, movers):
    text = "Daily brief from " + when + ":\n" + brief + "\nBiggest moves then:\n"
    for m in movers:
        text += f"- {m['event']} / {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}\n"
    save([("brief:" + when, "brief", "Brief from " + when, text)])


def add_comment(comment_id, username, market_title, text):
    save([("comment:" + str(comment_id), "comment", username + " on " + market_title,
           username + " commented on " + market_title + ": " + text)])


def delete_comment(comment_id):
    db.run("DELETE FROM docs WHERE id = %s", ("comment:" + str(comment_id),))


def count():
    return db.query("SELECT COUNT(*) FROM docs")[0][0]


def search(question, n=5):
    vectors = embed([question], "RETRIEVAL_QUERY")
    if not vectors:
        return []
    # <=> is pgvector's "cosine distance", smaller means closer in meaning
    rows = db.query("SELECT text, type, title FROM docs ORDER BY embedding <=> %s::vector LIMIT %s",
                    (to_vector(vectors[0]), n))
    hits = []
    for text, doc_type, title in rows:
        hits.append({"text": text, "type": doc_type, "title": title})
    return hits


# quick test: python rag.py
if __name__ == "__main__":
    print(count(), "documents")
    for q in ["How does the Dallas game settle if it's a tie?", "What moved the most recently?"]:
        print("\n" + q)
        for hit in search(q, 3):
            print("  -", hit["type"], "|", hit["title"])
