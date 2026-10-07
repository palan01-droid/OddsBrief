# the model starts by trusting the market, like it already saw 20 results at the market's price.
# the more real results it sees, the more it trusts its own numbers instead
PRIOR = 20


def bucket(p):
    # 0.00-0.09 -> 0, 0.10-0.19 -> 1, ... 0.90-1.00 -> 9
    b = int(p * 10)
    if b > 9:
        b = 9
    return b


class Calibrator:
    # learns how often a market priced at X% really happens, for each category
    def __init__(self):
        self.by_cat = {}   # (category, bucket) -> [times it happened, total]
        self.overall = {}  # bucket -> [times it happened, total]

    def counts(self, table, key):
        if key not in table:
            table[key] = [0, 0]
        return table[key]

    def predict(self, price, category):
        b = bucket(price)
        # first mix the market price with what we've seen across all categories
        yes, n = self.counts(self.overall, b)
        overall = (yes + PRIOR * price) / (n + PRIOR)
        # then mix that with what we've seen in this category
        yes, n = self.counts(self.by_cat, (category, b))
        return (yes + PRIOR * overall) / (n + PRIOR)

    def update(self, price, category, happened):
        b = bucket(price)
        cat = self.counts(self.by_cat, (category, b))
        cat[0] += happened
        cat[1] += 1
        total = self.counts(self.overall, b)
        total[0] += happened
        total[1] += 1


def train(db):
    rows = db.execute("""
        SELECT category, forecast, result = 'yes' FROM markets
        WHERE result IS NOT NULL AND forecast >= 0
        ORDER BY close_time
    """).fetchall()

    # score each market before learning from it, so the model never sees the answer first
    cal = Calibrator()
    market_err = 0
    model_err = 0
    for category, price, happened in rows:
        market_err += (price - happened) ** 2
        model_err += (cal.predict(price, category) - happened) ** 2
        cal.update(price, category, happened)

    n = len(rows)
    score = {"n": n, "market": None, "model": None}
    if n > 0:
        score["market"] = market_err / n
        score["model"] = model_err / n
    return cal, score


def calibration_table(cal):
    rows = []
    for b in range(10):
        if b in cal.overall:
            yes, n = cal.overall[b]
            label = str(b * 10) + "-" + str(b * 10 + 10) + "%"
            rows.append((label, yes / n, n))
    return rows


# quick test: python learn.py
if __name__ == "__main__":
    cal = Calibrator()
    assert abs(cal.predict(0.8, "Sports") - 0.8) < 1e-9
    for i in range(200):
        cal.update(0.8, "Sports", i % 2)
    p = cal.predict(0.8, "Sports")
    assert 0.5 < p < 0.6, p
    assert cal.predict(0.8, "Crypto") < 0.8
    print("learn.py ok")
