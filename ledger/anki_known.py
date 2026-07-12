"""Live Anki known-set recompute (replaces any stored export / resync step).

Adapted from the sentence-mining skill's load_known_intervals: for each
configured source (an Anki search + a note field), pull the cards via
AnkiConnect, tokenize the field with SudachiPy mode C, and record every
content lemma's HIGHEST card interval. A lemma is Anki-known once that
interval ≥ known_words.interval_threshold (default 21d).

This is recomputed each session and cached ~6h on disk (DESIGN.md — don't
persist what Anki already stores). The ledger unions this set with its own
promoted lemmas at materialize-known.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import lemma as L  # noqa: E402


def anki_request(action, url="http://localhost:8765", **params):
    """AnkiConnect call with retry-with-backoff on transient socket errors."""
    body = json.dumps({"action": action, "version": 6, "params": params}).encode()
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
            if resp.get("error"):
                raise RuntimeError(f"AnkiConnect: {resp['error']}")
            return resp["result"]
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"AnkiConnect failed after 3 attempts: {last_exc}")


def load_known_intervals(sources, url="http://localhost:8765"):
    """Compute {lemma: highest_interval} and {normalized: highest_interval}.

    New/learning cards report interval <= 0, so they fall below the known
    threshold naturally.
    """
    intervals = {}
    norm_intervals = {}
    for src in sources or []:
        query = (src.get("query") or "").strip()
        field = (src.get("field") or "").strip()
        if not query or not field:
            continue
        card_ids = anki_request("findCards", url=url, query=query)
        if not card_ids:
            print(f"  known-source matched 0 cards: {query!r}", file=sys.stderr)
            continue
        seen_field = False
        for i in range(0, len(card_ids), 500):
            chunk = anki_request("cardsInfo", url=url, cards=card_ids[i:i + 500])
            for c in chunk:
                ivl = c.get("interval", 0) or 0
                fobj = c.get("fields", {}).get(field)
                if fobj is None:
                    continue
                seen_field = True
                text = L.strip_html(L.strip_furigana(fobj.get("value", "")))
                if not text:
                    continue
                toks = L.tokenize(text)
                for ti in L.vocab_indices(toks):
                    t = toks[ti]
                    if ivl > intervals.get(t.lemma, -(10 ** 9)):
                        intervals[t.lemma] = ivl
                    if t.normalized and ivl > norm_intervals.get(t.normalized, -(10 ** 9)):
                        norm_intervals[t.normalized] = ivl
        if not seen_field:
            print(
                f"  WARNING: field {field!r} not found on cards for {query!r} "
                f"(check field name in config.known_words)",
                file=sys.stderr,
            )
    return intervals, norm_intervals


def _sources_key(sources):
    blob = json.dumps(sources, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def get_known_intervals(cfg, force_refresh=False):
    """load_known_intervals with the ~6h on-disk cache.

    Scanning ~13k cards + tokenizing takes ~100s; cache under work_dir keyed
    on the exact sources list (editing config auto-invalidates) and the
    threshold. TTL = known_words.cache_hours (default 6; 0 disables).
    """
    kw = cfg["known_words"]
    sources = kw.get("sources", [])
    threshold = kw.get("interval_threshold", 21)
    cache_hours = kw.get("cache_hours", 6)
    url = cfg.get("anki_connect_url", "http://localhost:8765")
    work_dir = os.path.expanduser(cfg.get("work_dir") or "~/immersion")
    cache_path = os.path.join(work_dir, ".known_cache.json")
    key = _sources_key(sources)

    if not force_refresh and cache_hours and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            age_h = (time.time() - cached.get("ts", 0)) / 3600
            if (cached.get("sources_key") == key
                    and cached.get("threshold") == threshold
                    and "norm_intervals" in cached
                    and age_h < cache_hours):
                print(f"  known-set: cache hit ({age_h:.1f}h old, "
                      f"{len(cached['intervals'])} lemmas)", file=sys.stderr)
                return cached["intervals"], cached["norm_intervals"]
        except Exception:  # corrupt cache → just rescan
            pass

    intervals, norm_intervals = load_known_intervals(sources, url=url)
    if cache_hours:
        try:
            os.makedirs(work_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sources_key": key,
                    "threshold": threshold,
                    "ts": time.time(),
                    "intervals": intervals,
                    "norm_intervals": norm_intervals,
                }, f, ensure_ascii=False)
        except Exception:
            pass
    return intervals, norm_intervals


def compute_anki_known(cfg, force_refresh=False):
    """The live-Anki-known sets, ready for KnownSet / materialize-known.

    Returns (known_lemmas, norm_known, known_stems) — lemmas whose highest
    card interval meets the threshold, their normalized variants, and their
    kanji stems.
    """
    threshold = cfg["known_words"].get("interval_threshold", 21)
    intervals, norm_intervals = get_known_intervals(cfg, force_refresh=force_refresh)
    known = {lem for lem, ivl in intervals.items() if ivl >= threshold}
    norm_known = {n for n, ivl in norm_intervals.items() if ivl >= threshold}
    stems = {s for s in (L.extract_kanji_stem(lem) for lem in known) if s}
    return known, norm_known, stems
