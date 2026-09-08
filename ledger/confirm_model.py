"""The adaptive think-you-know scorer (DESIGN.md — Adaptive confirm model).

A logistic regression over the snapshot the ledger takes of a word at every
knowledge claim (ledgerctl.claim_snapshot): how the word had been met up to
that moment. Trained on the claims that judged a *listed* word — the exposure
prompt's yes / not-yet answers and ✓ / ✗ marks made on a blue word — so the
population it is fit on is the population it scores: words that cleared θ.
Pure Python (the venv has no numpy); a few hundred rows × 16 features fits
in well under a second.

Outliers are expected and left in: words known before this system existed
show up as ✓ at first sight (first_sight = 1) — a feature, so the fit can
tell "already knew it" from "learned it here" instead of being skewed by it.
"""

import math
import random

FEATURES = (
    "log_rank",        # ln(freq rank); absent → ln(50000)
    "eps",             # watched episodes the word appeared in
    "q",               # exposures clearing the qualifying bar (≤1 other gap)
    "q0",              # exposures where it was the only gap
    "kr_mean",         # mean known_ratio of the sentences it was met in
    "kr_max",
    "occ",             # occurrences over those episodes (1 per row when unstamped)
    "lookups",         # popup opens with no mark
    "lookups_listed",  # …while it sat on a list
    "days_first",      # days since first watched exposure
    "days_last",       # days since the latest one
    "defers",          # prior "not yet" answers
    "is_verb",
    "is_kana",
    "length",          # lemma length in characters
    "first_sight",     # claim made with ≤1 watched exposure
)


def vectorize(snap):
    """Feature vector (FEATURES order) from a claim snapshot dict."""
    rank = snap.get("rank")
    eps = float(snap.get("eps") or 0)
    return [
        math.log(rank) if rank else math.log(50000.0),
        eps,
        float(snap.get("q") or 0),
        float(snap.get("q0") or 0),
        float(snap.get("kr_mean") or 0.0),
        float(snap.get("kr_max") or 0.0),
        math.log1p(float(snap.get("occ") or 0)),
        math.log1p(float(snap.get("lookups") or 0)),
        math.log1p(float(snap.get("lookups_listed") or 0)),
        math.log1p(max(0.0, float(snap.get("days_first") or 0.0))),
        math.log1p(max(0.0, float(snap.get("days_last") or 0.0))),
        float(snap.get("defers") or 0),
        1.0 if snap.get("is_verb") else 0.0,
        1.0 if snap.get("is_kana") else 0.0,
        float(snap.get("length") or 0),
        1.0 if eps <= 1 else 0.0,
    ]


def _standardize(X):
    n, d = len(X), len(X[0])
    means = [sum(row[j] for row in X) / n for j in range(d)]
    stds = []
    for j in range(d):
        v = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(math.sqrt(v) if v > 1e-12 else 1.0)
    return means, stds


def _apply(X, means, stds):
    return [[(x - m) / s for x, m, s in zip(row, means, stds)] for row in X]


def _sigmoid(z):
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _fit_raw(Z, y, l2=1.0, iters=300, lr=0.1):
    """Gradient-descent logistic regression on standardized rows."""
    n, d = len(Z), len(Z[0])
    w = [0.0] * d
    b = 0.0
    for _ in range(iters):
        gw = [0.0] * d
        gb = 0.0
        for row, t in zip(Z, y):
            p = _sigmoid(sum(wi * x for wi, x in zip(w, row)) + b)
            err = p - t
            gb += err
            for j in range(d):
                gw[j] += err * row[j]
        for j in range(d):
            w[j] -= lr * (gw[j] / n + l2 * w[j] / n)
        b -= lr * gb / n
    return w, b


def predict(model, snap):
    """P(known) for one snapshot under a fitted model dict."""
    z = _apply([vectorize(snap)], model["means"], model["stds"])[0]
    return _sigmoid(sum(wi * x for wi, x in zip(model["weights"], z)) + model["bias"])


def _auc(probs, y):
    pos = [p for p, t in zip(probs, y) if t]
    neg = [p for p, t in zip(probs, y) if not t]
    if not pos or not neg:
        return None
    s = sum((1.0 if p > q else 0.5 if p == q else 0.0) for p in pos for q in neg)
    return s / (len(pos) * len(neg))


def _threshold_for(probs, y, target, min_pos=10):
    """Smallest cutoff whose precision over rows scoring ≥ it reaches
    `target` with at least `min_pos` predicted positives; None if no cutoff
    does (then the caller keeps the hand gate)."""
    order = sorted(zip(probs, y), key=lambda t: -t[0])
    best = None
    tp = n = 0
    for i, (p, t) in enumerate(order):
        n += 1
        tp += 1 if t else 0
        # only cut between distinct scores
        if i + 1 < len(order) and order[i + 1][0] == p:
            continue
        if n >= min_pos and tp / n >= target:
            best = p
    return best


def fit(rows, target=0.8, folds=5, seed=7):
    """rows: [(snapshot, label)] → model dict (weights over FEATURES,
    standardization, the precision cutoff, and out-of-fold metrics). The
    cutoff is picked on out-of-fold predictions so it is honest about new
    words; the final weights are fit on everything."""
    X = [vectorize(s) for s, _ in rows]
    y = [1.0 if t else 0.0 for _, t in rows]
    n = len(rows)
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    oof = [0.0] * n
    if n >= 2 * folds and sum(y) and n - sum(y):
        for k in range(folds):
            test = set(idx[k::folds])
            tr = [i for i in range(n) if i not in test]
            means, stds = _standardize([X[i] for i in tr])
            w, b = _fit_raw(_apply([X[i] for i in tr], means, stds), [y[i] for i in tr])
            for i in test:
                z = _apply([X[i]], means, stds)[0]
                oof[i] = _sigmoid(sum(wi * x for wi, x in zip(w, z)) + b)
        cutoff = _threshold_for(oof, y, target)
        auc = _auc(oof, y)
    else:
        cutoff, auc = None, None
    means, stds = _standardize(X)
    w, b = _fit_raw(_apply(X, means, stds), y)
    metrics = {"n": n, "positives": int(sum(y)), "auc": round(auc, 3) if auc is not None else None,
               "target_precision": target}
    if cutoff is not None:
        sel = [(p, t) for p, t in zip(oof, y) if p >= cutoff]
        metrics["precision"] = round(sum(t for _, t in sel) / len(sel), 3)
        metrics["recall"] = round(sum(t for _, t in sel) / sum(y), 3)
        metrics["flagged_share"] = round(len(sel) / n, 3)
    return {"features": list(FEATURES), "weights": [round(x, 5) for x in w],
            "bias": round(b, 5), "means": means, "stds": stds,
            "cutoff": cutoff, "metrics": metrics}
