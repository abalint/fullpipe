#!/usr/bin/env python3
"""Import the pre-app "Japanese study tracker" spreadsheet (printed to PDF)
into the ledger's immersion-time log (view_sessions, source='import').

The workbook is a Google-Sheets print: tabs in page order. Two carry time:

  Listening  (pages 1..N)   one row per show per day — Show · type · #eps ·
                            ~length · TOTAL MINUTES · date · daily hours …
                            Active immersion (anime, YouTube, audiobooks,
                            podcasts) → kind "watch" (the app's active bucket).
  passive    (one page)     Title · #eps · ~length · TOTAL MINUTES · date —
                            background listening → kind "listen".

Pages are found by their header line, so tab order/length doesn't matter.
The text is pypdf's layout-mode extraction; cells are whitespace-separated
except where a long title runs into the numbers ("…アニメ10202003/7/25"),
which the parser untangles (eps × length == total). The Total column is
authoritative; eps/length only disambiguate.

Sheet repairs applied (each one verified against the sheet's own printed
weekly totals):
  · a date typed out of order (6/2/26 amid April rows; 3/2/26 amid March 6→8)
    becomes the day after the previous row
  · consecutive daily "misc" rows sharing one date at the tail of the log
    (the date column stopped being updated) become consecutive days
  · a title's trailing digits jammed onto the numbers ("[thCqYRsS6DY]4040" =
    40 min, not 4040) — a total > 12 h that splits into equal halves is one half

Idempotent: ids are a hash of (tab, row order, content); re-running is a
no-op, --replace first drops every source='import' row.

    python3 -m tools.import_tracker_pdf PDF [--dry-run] [--replace] [--config PATH]

Needs pypdf (pip install pypdf).
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

# run as `python3 -m tools.import_tracker_pdf`; when invoked by path, drop
# tools/ from sys.path — tools/select.py would shadow the stdlib module
if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    sys.path.pop(0)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TYPES = ["audio book", "K drama", "アニメ", "映画", "youtube", "podcast", "ニュース",
         "ゲーム", "TV", "tv", "drama", "misc", "VN", "audiobook"]
TYPE_RE = "|".join(re.escape(t) for t in sorted(TYPES, key=len, reverse=True))
DATE_RE = re.compile(r"(1[0-2]|[1-9])/(3[01]|[12]\d|[1-9])/(2[5-9])")
NUM = re.compile(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![A-Za-z\d])")
LISTENING_HEADER = re.compile(r"^Show\s+type\s+# of episodes")
PASSIVE_HEADER = re.compile(r"^Title\s+# of episodes\s+~Length")
OTHER_HEADER = re.compile(r"^(Title\s+Type\s+# of pages|person\s+minutes|Type\s+Time in minutes|"
                          r"# of reps|hours diff)")


def split_jammed(s):
    """'10202003' → (10, 20, 200) where eps × len == total; None if no split."""
    s = s.replace(" ", "")
    if not re.fullmatch(r"[\d.]+", s):
        return None
    n = len(s)
    for i in range(1, n):
        for j in range(i + 1, n):
            a, b, c = s[:i], s[i:j], s[j:]
            if any(x.startswith("0") and len(x) > 1 and not x.startswith("0.") for x in (a, b, c)):
                continue
            try:
                fa, fb, fc = float(a), float(b), float(c)
            except ValueError:
                continue
            if fa > 0 and abs(fa * fb - fc) < 0.01:
                return fa, fb, fc
    return None


def split_halves(tok):
    """'4040' → 40.0, '27.527.5' → 27.5: a token that is one number written
    twice (length and total jammed together)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\1", tok)
    return float(m.group(1)) if m else None


def parse_line(line, prev_day):
    """One layout line → row dict or None. prev_day disambiguates which of
    several date-looking substrings is the date column (the log is
    chronological)."""
    cands = []
    for m in DATE_RE.finditer(line):
        try:
            cands.append((m, dt.date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))))
        except ValueError:
            continue
    if not cands:
        return None
    m, day = next(((m, d) for m, d in cands if prev_day is None or d >= prev_day), cands[0])
    head = line[:m.start()]
    typ, title, nums_s = None, head.strip(), ""
    for tm in reversed(list(re.finditer(r"(" + TYPE_RE + r")", head))):
        tail = head[tm.end():]
        if re.fullmatch(r"[\s\d.:]*", tail) or NUM.search(tail):
            typ, title, nums_s = tm.group(1), head[:tm.start()].strip(), tail
            break
    if typ is None:
        mm = re.match(r"^(.*?)([\d\s.:]*)$", head)
        title, nums_s = mm.group(1).strip(), mm.group(2)
    if not title and typ:  # a bare "misc" row: the word is the title, not a type
        title, typ = typ, None
    cleaned = re.sub(r"\d+:\d\d", " ", nums_s)  # h:mm lengths are not counts
    toks = NUM.findall(cleaned)
    # a jammed 'length+total' tail: last token is a number written twice
    if toks and split_halves(toks[-1]) is not None and float(toks[-1]) > 720:
        half = split_halves(toks[-1])
        toks = toks[:-1] + [str(half), str(half)]
    eps = length = total = None
    if len(toks) >= 3:
        eps, length, total = (float(x) for x in toks[-3:])
    elif len(toks) == 2:
        length, total = float(toks[0]), float(toks[1])
    elif len(toks) == 1:
        j = split_jammed(cleaned) if len(cleaned.strip()) > 3 else None
        if j:
            eps, length, total = j
        else:
            total = float(toks[0])
    if total is None or total <= 0:
        return None
    return {"title": title, "type": typ, "eps": eps, "len": length,
            "min": total, "day": day, "line": line.strip()}


DAILY_AGGREGATE_TITLES = ("podcasts", "misc")


def repair_dates(rows):
    """Chronology fixes (see module doc). Mutates and returns rows. Only the
    one-row-per-day aggregate era (title 'podcasts'/'misc') is touched — in
    the show-log era a row dated earlier than its neighbours is a late
    entry, not a typo, and stays where it was typed. One pass, each row
    judged against the previous row's (already repaired) day, so a run of
    stale tail dates walks forward a day at a time."""
    for i in range(1, len(rows)):
        r, p = rows[i], rows[i - 1]
        if r["title"] not in DAILY_AGGREGATE_TITLES:
            continue
        prev = p["day"]
        nxt = rows[i + 1]["day"] if i + 1 < len(rows) else None
        out_of_order = r["day"] < prev
        stale_repeat = (r["day"] == prev and r["title"] == "misc"
                        and p["title"] == "misc" and r["min"] > 0)
        far_forward = nxt is not None and r["day"] > nxt and (r["day"] - prev).days > 40
        if not (out_of_order or stale_repeat or far_forward):
            continue
        fixed = prev + dt.timedelta(days=1)
        if far_forward and nxt is not None and fixed > nxt:
            fixed = prev
        r["day"] = fixed
        r["repaired"] = "date"
    return rows


def parse_pages(page_texts):
    """{'listening': rows, 'passive': rows} from the PDF's per-page layout text."""
    tabs = {"listening": [], "passive": []}
    current = None
    for text in page_texts:
        lines = text.splitlines()
        # a header line names the tab; pages without one continue the last tab
        for l in lines[:3]:
            if LISTENING_HEADER.search(l.strip()):
                current = "listening"
            elif PASSIVE_HEADER.search(l.strip()):
                current = "passive"
            elif OTHER_HEADER.search(l.strip()):
                current = None
        if current is None:
            continue
        prev = tabs[current][-1]["day"] if tabs[current] else None
        for l in lines:
            row = parse_line(l, prev)
            if row:
                tabs[current].append(row)
                prev = row["day"]
    for rows in tabs.values():
        repair_dates(rows)
    return tabs


def to_sessions(tabs):
    """Rows → view_sessions payloads. Listening tab = active ('watch'),
    passive tab = 'listen'. Deterministic ids for idempotent re-runs."""
    out = []
    for tab, kind in (("listening", "watch"), ("passive", "listen")):
        for i, r in enumerate(tabs[tab]):
            key = f"{tab}|{i}|{r['title']}|{r['type']}|{r['min']}|{r['line']}"
            sid = "imp_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            title = r["title"] or (r["type"] or "immersion")
            if r["type"] and r["type"] != r["title"]:
                title = f"{title} ({r['type']})"
            out.append({
                "id": sid, "episode_id": f"import_{tab}", "title": title, "kind": kind,
                "day": r["day"].isoformat(), "start": f"{r['day'].isoformat()}T00:00:00",
                "secs": round(r["min"] * 60, 1), "reached": 0, "duration": None,
                "source": "import",
            })
    return out


def page_texts_from_pdf(path):
    import pypdf
    reader = pypdf.PdfReader(str(path))
    return [p.extract_text(extraction_mode="layout") or "" for p in reader.pages]


def summarize(tabs):
    hrs = lambda rows: round(sum(r["min"] for r in rows) / 60, 2)
    lis, pas = tabs["listening"], tabs["passive"]
    rep = [r for r in lis + pas if r.get("repaired")]
    months = collections.Counter()
    for r in lis:
        months[r["day"].strftime("%Y-%m")] += r["min"] / 60
    return {
        "listening": {"rows": len(lis), "hours": hrs(lis),
                      "from": min(r["day"] for r in lis).isoformat() if lis else None,
                      "to": max(r["day"] for r in lis).isoformat() if lis else None},
        "passive": {"rows": len(pas), "hours": hrs(pas)},
        "repaired": [{"title": r["title"][:30], "min": r["min"], "day": r["day"].isoformat()}
                     for r in rep],
        "by_month": {k: round(v, 1) for k, v in sorted(months.items())},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdf")
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="parse + reconcile, write nothing")
    ap.add_argument("--replace", action="store_true",
                    help="drop existing source='import' rows first")
    ap.add_argument("--json", help="also dump the parsed sessions here")
    args = ap.parse_args(argv)

    tabs = parse_pages(page_texts_from_pdf(args.pdf))
    sessions = to_sessions(tabs)
    print(json.dumps(summarize(tabs), ensure_ascii=False, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(sessions, ensure_ascii=False, indent=0),
                                   encoding="utf-8")
    if args.dry_run:
        print(f"dry run — {len(sessions)} sessions parsed, nothing written")
        return
    from ledger import ledgerctl as lc
    from lib_config import load_config
    cfg = load_config(args.config)
    conn = lc.open_db(cfg["ledger_db"])
    if args.replace:
        n = conn.execute("DELETE FROM view_sessions WHERE source = 'import'").rowcount
        conn.commit()
        print(f"dropped {n} previously imported rows")
    new = dup = 0
    for s in sessions:
        if lc.record_view_session(conn, s)["duplicate"]:
            dup += 1
        else:
            new += 1
    print(f"imported {new} sessions ({dup} already present)")


if __name__ == "__main__":
    main()
