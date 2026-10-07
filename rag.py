import chromadb

# same setup as ai-tutor-rag: chroma's built-in local embedding model, so searching costs nothing
client = chromadb.PersistentClient(path="./chroma_db")
docs = client.get_or_create_collection(name="oddsbrief")


def add_rules(markets):
    # only embed markets we haven't seen yet, rules don't change
    ids = [f"rules:{m['ticker']}" for m in markets]
    have = set(docs.get(ids=ids)["ids"])
    new = [(i, m) for i, m in zip(ids, markets) if i not in have and m.get("rules")]
    if not new:
        return 0
    docs.add(
        ids=[i for i, m in new],
        documents=[f"Market rules for {m['event']} - {m['pick']}:\n{m['rules']}" for i, m in new],
        metadatas=[{"type": "rules", "title": f"{m['event']} - {m['pick']}"} for i, m in new],
    )
    return len(new)


def add_brief(when, brief, movers):
    moves = "\n".join(f"- {m['event']} / {m['pick']}: {m['prev']:.0%} -> {m['price']:.0%}" for m in movers)
    docs.upsert(
        ids=[f"brief:{when}"],
        documents=[f"Daily brief from {when}:\n{brief}\nBiggest moves then:\n{moves}"],
        metadatas=[{"type": "brief", "title": f"Brief from {when}"}],
    )


def add_comment(comment_id, username, market_title, text):
    docs.upsert(
        ids=[f"comment:{comment_id}"],
        documents=[f"{username} commented on {market_title}: {text}"],
        metadatas=[{"type": "comment", "title": f"{username} on {market_title}"}],
    )


def delete_comment(comment_id):
    docs.delete(ids=[f"comment:{comment_id}"])


def search(question, n=5):
    if docs.count() == 0:
        return []
    result = docs.query(query_texts=[question], n_results=min(n, docs.count()))
    return [{"text": d, "type": m["type"], "title": m["title"]}
            for d, m in zip(result["documents"][0], result["metadatas"][0])]


if __name__ == "__main__":
    print(docs.count(), "documents")
    for q in ["How does the Dallas game settle if it's a tie?", "What moved the most recently?"]:
        print("\n" + q)
        for hit in search(q, 3):
            print("  -", hit["type"], "|", hit["title"])
