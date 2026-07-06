#!/usr/bin/env python3
"""JMdict → SQLite for the player's any-word dictionary popup.

The mobile player looks words up by the *lemma* Sudachi already put on every
transcript token, so no deinflection machinery (the hard part of Yomitan) is
needed — just a key→entry table over JMdict_e (the same EDRDG dictionary
Yomitan ships). Entries are keyed by every kanji and reading form; lookups
return compact JSON the server relays verbatim.

Usage:
    python -m tools.jmdict build [--config PATH] [--xml JMdict_e(.gz)]
    python -m tools.jmdict lookup WORD [WORD ...]

`build` downloads JMdict_e.gz from EDRDG (CC BY-SA, ~9 MB) when --xml is not
given, and writes <work_dir>/jmdict.db (~40 MB). One-off; rerun to refresh.
"""

import argparse
import gzip
import json
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib_config import load_config  # noqa: E402

JMDICT_URL = "http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz"
MAX_SENSES = 8
MAX_GLOSSES = 5
DEFAULT_MAX_ENTRIES = 4  # per-lemma cap on lookup


def db_path(cfg):
    return Path(cfg["work_dir"]) / "jmdict.db"


def open_db(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _pri_score(elem):
    """Commonness from ke_pri/re_pri tags: each news1/ichi1/spec1/gai1 counts
    double, other tags (news2, nfXX, …) once. Higher = more common."""
    score = 0
    for pri in elem.findall("./ke_pri") + elem.findall("./re_pri"):
        score += 2 if pri.text and pri.text.endswith("1") else 1
    return score


def _short_pos(text):
    """Entity-expanded pos ("noun (common) (futsuumeishi)") → "noun"."""
    return (text or "").split(" (")[0]


def parse_entries(fileobj):
    """Yield (pri, keys, data) per <entry>; data is the compact wire dict
    {"k": kanji forms, "r": readings, "s": [{"pos": [...], "g": [...]}]}."""
    for _, entry in ET.iterparse(fileobj):
        if entry.tag != "entry":
            continue
        kanji = [k.text for k in entry.findall("./k_ele/keb") if k.text]
        kana = [r.text for r in entry.findall("./r_ele/reb") if r.text]
        senses = []
        for sense in entry.findall("./sense")[:MAX_SENSES]:
            glosses = [g.text for g in sense.findall("./gloss") if g.text]
            if not glosses:
                continue
            senses.append({
                "pos": [_short_pos(p.text) for p in sense.findall("./pos")],
                "g": glosses[:MAX_GLOSSES],
            })
        if senses and (kanji or kana):
            pri = max((_pri_score(e) for e in
                       entry.findall("./k_ele") + entry.findall("./r_ele")),
                      default=0)
            yield pri, set(kanji + kana), {"k": kanji, "r": kana, "s": senses}
        entry.clear()  # keep iterparse memory flat


def build_db(conn, entries):
    """(Re)build the two tables from a parse_entries stream. Returns count."""
    conn.executescript(
        "DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS keys;"
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, pri INTEGER NOT NULL,"
        "                      data TEXT NOT NULL);"
        "CREATE TABLE keys (key TEXT NOT NULL, id INTEGER NOT NULL);")
    n = 0
    for pri, keys, data in entries:
        n += 1
        conn.execute("INSERT INTO entries (id, pri, data) VALUES (?, ?, ?)",
                     (n, pri, json.dumps(data, ensure_ascii=False)))
        conn.executemany("INSERT INTO keys (key, id) VALUES (?, ?)",
                         [(k, n) for k in keys])
    conn.execute("CREATE INDEX idx_keys_key ON keys(key)")
    conn.commit()
    return n


def lookup_many(conn, lemmas, max_entries=DEFAULT_MAX_ENTRIES):
    """{lemma: [entry, …]} common-first; lemmas with no entry are absent."""
    out = {}
    for lemma in set(lemmas):
        rows = conn.execute(
            "SELECT e.data FROM keys k JOIN entries e ON e.id = k.id"
            " WHERE k.key = ? ORDER BY e.pri DESC, e.id LIMIT ?",
            (lemma, max_entries)).fetchall()
        if rows:
            out[lemma] = [json.loads(r[0]) for r in rows]
    return out


def build(cfg, xml_path=None):
    out = db_path(cfg)
    if xml_path is None:
        xml_path = Path(cfg["work_dir"]) / "JMdict_e.gz"
        if not xml_path.exists():
            print(f"downloading {JMDICT_URL} …", file=sys.stderr)
            urllib.request.urlretrieve(JMDICT_URL, xml_path)
    xml_path = Path(xml_path)
    opener = gzip.open if xml_path.suffix == ".gz" else open
    conn = sqlite3.connect(out)
    with opener(xml_path, "rb") as f:
        n = build_db(conn, parse_entries(f))
    conn.close()
    print(f"{out}: {n} entries")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="JMdict_e → <work_dir>/jmdict.db")
    b.add_argument("--xml", help="local JMdict_e(.gz); downloaded if omitted")
    q = sub.add_parser("lookup", help="debug: print entries for words")
    q.add_argument("words", nargs="+")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.cmd == "build":
        build(cfg, args.xml)
    else:
        conn = open_db(db_path(cfg))
        print(json.dumps(lookup_many(conn, args.words),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
