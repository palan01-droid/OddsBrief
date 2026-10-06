from collections import defaultdict

# how many "pretend" results at the market price before we trust our own data
PRIOR = 20


def bucket(p):
    return min(int(p * 10), 9)


class Calibrator:
    # learns how often a market priced at X% really happens, per category
    def __init__(self):
        self.by_cat = defaultdict(lambda: [0, 0])
        self.overall = defaultdict(lambda: [0, 0])

    def predict(self, price, category):
        b = bucket(price)
        yes, n = self.overall[b]
        overall = (yes + PRIOR * price) / (n + PRIOR)
        yes, n = self.by_cat[(category, b)]
        return (yes + PRIOR * overall) / (n + PRIOR)

    def update(self, price, category, happened):
        b = bucket(price)
        for table, key in ((self.by_cat, (category, b)), (self.overall, b)):
            table[key][0] += happened
            table[key][1] += 1


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
    score = {"n": n, "market": market_err / n if n else None, "model": model_err / n if n else None}
    return cal, score


def calibration_table(cal):
    rows = []
    for b in range(10):
        yes, n = cal.overall[b]
        if n:
            rows.append((f"{b * 10}-{b * 10 + 10}%", yes / n, n))
    return rows


if __name__ == "__main__":
    cal = Calibrator()
    assert abs(cal.predict(0.8, "Sports") - 0.8) < 1e-9
    for i in range(200):
        cal.update(0.8, "Sports", i % 2)
    p = cal.predict(0.8, "Sports")
    assert 0.5 < p < 0.6, p
    assert cal.predict(0.8, "Crypto") < 0.8
    print("learn.py ok")
