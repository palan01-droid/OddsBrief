import chromadb

# same setup as my ai-tutor-rag, chroma's built in embedding model runs locally so it's free
client = chromadb.PersistentClient(path="./chroma_db")
docs = client.get_or_create_collection(name="oddsbrief")


def add_rules(markets):
    # only add markets we haven't saved yet (rules don't change)
    all_ids = []
    for m in markets:
        all_ids.append("rules:" + m["ticker"])
    already_saved = docs.get(ids=all_ids)["ids"]

    ids = []
    texts = []
    info = []
    for m in markets:
        doc_id = "rules:" + m["ticker"]
        if doc_id in already_saved or not m.get("rules"):
            continue
        title = m["event"] + " - " + m["pick"]
        ids.append(doc_id)
        texts.append("Market rules for " + title + ":\n" + m["rules"])
        info.append({"type": "rules", "title": title})

    if len(ids) > 0:
        docs.add(ids=ids, documents=texts, metadatas=info)
    return len(ids)


def add_brief(when, brief, movers):
    text = "Daily brief from " + when + ":\n" + brief + "\nBiggest moves then:\n"
    for m in movers:
        text += f"- {m['event']} / {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}\n"
    docs.upsert(ids=["brief:" + when], documents=[text],
                metadatas=[{"type": "brief", "title": "Brief from " + when}])


def add_comment(comment_id, username, market_title, text):
    docs.upsert(ids=["comment:" + str(comment_id)],
                documents=[username + " commented on " + market_title + ": " + text],
                metadatas=[{"type": "comment", "title": username + " on " + market_title}])


def delete_comment(comment_id):
    docs.delete(ids=["comment:" + str(comment_id)])


def search(question, n=5):
    if docs.count() == 0:
        return []
    n = min(n, docs.count())
    result = docs.query(query_texts=[question], n_results=n)
    hits = []
    for i in range(len(result["documents"][0])):
        meta = result["metadatas"][0][i]
        hits.append({"text": result["documents"][0][i], "type": meta["type"], "title": meta["title"]})
    return hits


# quick test: python rag.py
if __name__ == "__main__":
    print(docs.count(), "documents")
    for q in ["How does the Dallas game settle if it's a tie?", "What moved the most recently?"]:
        print("\n" + q)
        for hit in search(q, 3):
            print("  -", hit["type"], "|", hit["title"])
