#!/usr/bin/env python3
"""harvest — the dumb discovery tool under /recommend (ytSearch Phase 1).

Pulls raw candidate videos from YouTube's *unauthenticated* graph and stashes
them in a discovery store (<work_dir>/discover.db), kept deliberately separate
from the ledger so crawl junk never touches the event-sourced truth
(ytSearch/DESIGN.md — ownership split). Three edges, all verified reachable
without login, key, account, or PO token (rec-system research 2026-07-06):

    related  — InnerTube /next secondaryResults (content-similarity rail)
    search   — yt-dlp ytsearchN: (where the skill's JP query-expansion lands)
    rss      — youtube.com/feeds/videos.xml (fresh uploads from known channels)

No judgment lives here. The /recommend skill (this Claude Code session — no
cloud LLM) reads `seeds`, expands taste into Japanese queries, drives `run`,
then reads `list` and ranks the pool. Candidates already in the ledger or
previously dismissed are filtered out so nothing re-surfaces.

CLI (run from $FULLPIPE with .venv/bin/python):
    python -m tools.harvest seeds
    python -m tools.harvest run [--related VID ...] [--search Q ...] [--rss CID ...]
    python -m tools.harvest list [--status new]
    python -m tools.harvest gate-speech VIDEO_ID ...   (drop speechless picks)
    python -m tools.harvest set-status VIDEO_ID {new|queued|dismissed|picked}
"""

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402  (already a project dependency)

from engine.paths import ytdlp_path, ytdlp_extra_args, _NOWWIN  # noqa: E402
from lib_config import load_config  # noqa: E402

# InnerTube WEB client. clientVersion drifts; bump it if /next starts returning
# empty or 400 (yt-dlp/youtubei.js bump theirs the same way). No PO token is
# needed for the metadata /next endpoint — only /player streaming is gated.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INNERTUBE_CLIENT_VERSION = "2.20250620.01.00"
STATUSES = ("new", "queued", "dismissed", "picked", "filtered", "no_speech")

# Synthetic-TTS-narrator formats to exclude by default (a personal taste filter —
# override/extend via config `discover.format_blocklist`). Matched case-insensitively
# as substrings of "title + channel". These formats brand themselves in the title
# or channel name, so a metadata match is high-precision. NOTE the ゆっくり entries
# are the *format compounds* (ゆっくり解説/実況/…) and the bracketed 【ゆっくり — never
# bare "ゆっくり", which also means "leisurely" (e.g. a ゆっくり散歩 walking vlog must
# NOT be filtered).
DEFAULT_FORMAT_BLOCKLIST = [
    "ゆっくり解説", "ゆっくり実況", "ゆっくり茶番", "ゆっくり劇場", "ゆっくり歴史",
    "ゆっくり地理", "ゆっくり科学", "ゆっくり雑学", "ゆっくり軍事", "ゆっくり動画",
    "ゆっくりボイス", "【ゆっくり", "ゆっくり】",
    "voiceroid", "ボイスロイド", "ボイロ", "voicevox", "ずんだもん",
    "結月ゆかり", "琴葉茜", "琴葉葵", "東北きりたん", "音街ウナ", "弦巻マキ",
    "cevio", "a.i.voice", "合成音声", "音声合成", "aquestalk", "softalk",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    video_id   TEXT PRIMARY KEY,
    title      TEXT,
    channel    TEXT,
    channel_id TEXT,
    duration   INTEGER,        -- seconds, when the edge reports it
    view_count INTEGER,        -- when the edge reports it
    edge       TEXT NOT NULL,  -- related | search | rss
    seed       TEXT,           -- seed video_id / query / channel_id that surfaced it
    status     TEXT NOT NULL DEFAULT 'new',   -- new | queued | dismissed | picked
    first_seen TEXT NOT NULL,
    meta       TEXT            -- JSON: extra bits (view text, description snippet)
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- discovery store ----------------------------------------------------------

def open_discover(db_path):
    import sqlite3
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def discover_db_path(cfg):
    return Path(cfg["work_dir"]) / "discover.db"


def load_blocklist(cfg):
    """(format substrings lowercased, channel_ids) from config, falling back to
    the built-in synthetic-TTS default when config says nothing."""
    disc = cfg.get("discover") or {}
    fmt = disc.get("format_blocklist")
    if fmt is None:
        fmt = DEFAULT_FORMAT_BLOCKLIST
    chan = disc.get("channel_blocklist") or []
    return [s.lower() for s in fmt], set(chan)


def is_blocked(cand, fmt_subs, chan_ids):
    if cand.get("channel_id") and cand["channel_id"] in chan_ids:
        return True
    hay = f"{cand.get('title') or ''} {cand.get('channel') or ''}".lower()
    return any(sub in hay for sub in fmt_subs)


# --- harvest edges (dumb, deterministic) --------------------------------------

def _innertube(endpoint, body):
    """POST to youtubei/v1/<endpoint>; return parsed JSON or None on any failure."""
    url = f"https://www.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false"
    try:
        r = requests.post(url, json=body,
                          headers={"Content-Type": "application/json", "User-Agent": UA},
                          timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def related(video_id, hl="ja", gl="JP"):
    """Related videos from the watch-next rail (InnerTube /next). A content +
    popularity similarity edge, not personalized (rec-system research)."""
    body = {"context": {"client": {"clientName": "WEB",
            "clientVersion": INNERTUBE_CLIENT_VERSION, "hl": hl, "gl": gl}},
            "videoId": video_id}
    data = _innertube("next", body)
    if not data:
        return []
    results = (data.get("contents", {}).get("twoColumnWatchNextResults", {})
               .get("secondaryResults", {}).get("secondaryResults", {})
               .get("results", []))
    out = []
    for item in results:
        # Current renderer is lockupViewModel; keep a compactVideoRenderer
        # fallback in case YouTube reverts.
        lv = item.get("lockupViewModel")
        if lv:
            vid = lv.get("contentId")
            md = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = md.get("title", {}).get("content")
            rows = (md.get("metadata", {}).get("contentMetadataViewModel", {})
                    .get("metadataRows", []))
            parts = [p.get("text", {}).get("content", "")
                     for row in rows for p in row.get("metadataParts", [])]
            channel = parts[0] if parts else None
            if vid and title:
                out.append({"video_id": vid, "title": title, "channel": channel,
                            "channel_id": None, "duration": None, "view_count": None,
                            "edge": "related", "seed": video_id,
                            "meta": {"info": parts[1:]} if len(parts) > 1 else {}})
            continue
        cvr = item.get("compactVideoRenderer")
        if cvr:
            vid = cvr.get("videoId")
            title = "".join(r.get("text", "") for r in cvr.get("title", {}).get("runs", []))
            channel = "".join(r.get("text", "") for r in
                              cvr.get("longBylineText", {}).get("runs", []))
            if vid and title:
                out.append({"video_id": vid, "title": title, "channel": channel or None,
                            "channel_id": None, "duration": None, "view_count": None,
                            "edge": "related", "seed": video_id, "meta": {}})
    return out


def search(query, n=8):
    """yt-dlp unauthenticated search — the landing pad for the skill's
    JP-native query expansion (the highest-leverage AI step, ytSearch design)."""
    cmd = [ytdlp_path(), *ytdlp_extra_args(), "--flat-playlist", "--dump-json",
           f"ytsearch{n}:{query}"]
    result = subprocess.run(cmd, capture_output=True, text=True, **_NOWWIN)
    out = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = d.get("id")
        if not vid:
            continue
        out.append({"video_id": vid, "title": d.get("title"),
                    "channel": d.get("channel") or d.get("uploader"),
                    "channel_id": d.get("channel_id") or d.get("uploader_id"),
                    "duration": d.get("duration"), "view_count": d.get("view_count"),
                    "edge": "search", "seed": query, "meta": {}})
    return out


def channel_rss(channel_id):
    """Latest ~15 uploads from a channel's Atom feed. No auth, no bot-check."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    ns = {"a": "http://www.w3.org/2005/Atom",
          "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", default=None, namespaces=ns)
        title = e.findtext("a:title", default=None, namespaces=ns)
        author = e.find("a:author/a:name", ns)
        channel = author.text if author is not None else None
        published = e.findtext("a:published", default=None, namespaces=ns)
        if vid:
            out.append({"video_id": vid, "title": title, "channel": channel,
                        "channel_id": channel_id, "duration": None, "view_count": None,
                        "edge": "rss", "seed": channel_id,
                        "meta": {"published": published} if published else {}})
    return out


# --- ledger read (seeds + the exclusion set) ----------------------------------

def _ledger_conn(cfg):
    import sqlite3
    db = cfg["ledger_db"]
    if not Path(db).exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def known_video_ids(cfg):
    """YouTube video ids already in the ledger — never recommend these back."""
    con = _ledger_conn(cfg)
    ids = set()
    if con is None:
        return ids
    try:
        for (eid,) in con.execute("SELECT id FROM episodes"):
            if eid and eid.startswith("yt_"):
                ids.add(eid[3:])
    finally:
        con.close()
    return ids


def gather_seeds(cfg):
    """The ledger-derived bootstrap the skill reasons over: the rated history
    (for taste + ranking), the channels behind it (RSS seeds), and liked video
    ids (related-video expansion seeds)."""
    con = _ledger_conn(cfg)
    if con is None:
        return {"rated": [], "channels": [], "liked_video_ids": [], "watched_count": 0}
    try:
        # Latest tag set per episode (re-rating appends a new review_id batch).
        latest_reviews = {
            ep: rid for ep, rid in con.execute(
                "SELECT episode_id, review_id FROM taste_events "
                "WHERE kind='rating' AND id IN "
                "(SELECT MAX(id) FROM taste_events WHERE kind='rating' GROUP BY episode_id)")
        }
        tags_by_ep = {}
        if latest_reviews:
            qs = ",".join("?" * len(latest_reviews))
            for rid, val in con.execute(
                    f"SELECT review_id, value FROM taste_events "
                    f"WHERE kind='tag' AND review_id IN ({qs})",
                    tuple(latest_reviews.values())):
                ep = next((e for e, r in latest_reviews.items() if r == rid), None)
                if ep:
                    tags_by_ep.setdefault(ep, []).append(val)

        rated = []
        for row in con.execute(
                "SELECT id, title, channel, channel_id, rating, genre, format, "
                "json_extract(metadata,'$.topics') AS topics "
                "FROM episodes WHERE rating IS NOT NULL "
                "ORDER BY rating DESC, processed_at DESC"):
            topics = []
            if row[7]:
                try:
                    topics = json.loads(row[7])
                except (json.JSONDecodeError, TypeError):
                    topics = []
            rated.append({
                "video_id": row[0][3:] if row[0].startswith("yt_") else row[0],
                "title": row[1], "channel": row[2], "channel_id": row[3],
                "rating": row[4], "tags": tags_by_ep.get(row[0], []),
                "genre": row[5], "format": row[6], "topics": topics})

        channels = []
        for ch, cid, best in con.execute(
                "SELECT channel, channel_id, MAX(rating) FROM episodes "
                "WHERE channel_id IS NOT NULL GROUP BY channel_id "
                "ORDER BY MAX(rating) DESC NULLS LAST"):
            channels.append({"channel": ch, "channel_id": cid, "best_rating": best})

        liked = [r["video_id"] for r in rated if (r["rating"] or 0) >= 4]
        (watched_count,) = con.execute(
            "SELECT COUNT(*) FROM episodes WHERE watched=1").fetchone()
        return {"rated": rated, "channels": channels, "liked_video_ids": liked,
                "watched_count": watched_count}
    finally:
        con.close()


# --- store + filter -----------------------------------------------------------

def store_candidates(conn, cands, exclude_ids, blocklist=None):
    """Insert genuinely-new candidates (not in the ledger, not already seen in
    the discovery store, deduped within the batch). Blocklisted formats are still
    stored — but as status='filtered', so they never surface as 'new' yet stay
    auditable (ytSearch design: log what you prune). Returns (new_rows, n_filtered)."""
    fmt_subs, chan_ids = blocklist or ([], set())
    new_rows, batch_seen, n_filtered = [], set(), 0
    ts = now_iso()
    for c in cands:
        vid = c.get("video_id")
        if not vid or vid in exclude_ids or vid in batch_seen:
            continue
        batch_seen.add(vid)
        if conn.execute("SELECT 1 FROM candidates WHERE video_id=?", (vid,)).fetchone():
            continue  # already discovered (any status) — don't resurface
        blocked = is_blocked(c, fmt_subs, chan_ids)
        status = "filtered" if blocked else "new"
        conn.execute(
            "INSERT INTO candidates (video_id, title, channel, channel_id, duration, "
            "view_count, edge, seed, status, first_seen, meta) "
            f"VALUES (?,?,?,?,?,?,?,?, '{status}', ?, ?)",
            (vid, c.get("title"), c.get("channel"), c.get("channel_id"),
             c.get("duration"), c.get("view_count"), c.get("edge"), c.get("seed"),
             ts, json.dumps(c.get("meta") or {}, ensure_ascii=False)))
        if blocked:
            n_filtered += 1
        else:
            new_rows.append({**c, "status": "new"})
    conn.commit()
    return new_rows, n_filtered


def probe_speech(video_id, timeout=90):
    """Ask yt-dlp what the video's spoken language is — the deterministic speech
    gate under /recommend. A pick is only useful for immersion if it contains
    *Japanese speech* to mine; wordless / music-only / ambient footage (a 整地
    work video, a ジオラマ build) yields nothing, however well it fits the taste.

    Signal (validated 2026-07 against silent vs. speech samples): YouTube's ASR
    emits an automatic-caption track suffixed `-orig` only for the language it
    actually *heard*, and sets the `language` field from it. So Japanese speech
    ⇔ `language == 'ja'` or a `ja-orig` auto-caption exists. Two traps this
    deliberately avoids: a plain `ja` auto-caption is often just an auto-*trans-
    lation* of foreign narration (false positive), and manual `ja` subtitles can
    be uploader-added text on a silent video (also false positive) — so neither
    counts. Never raises; returns a verdict dict.

    verdict ∈ {"ja", "non_ja", "silent", "unknown"}:
      ja      — Japanese speech present                         → keep
      non_ja  — speech detected but not Japanese                → drop
      silent  — no speech detected at all (music/ambient)       → drop
      unknown — probe failed (private/geo/removed/network)      → don't drop
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    # `-t sleep` throttles to stay under YouTube's rate limiter — gate_speech
    # calls this once per candidate, so the per-request sleep matters at volume.
    cmd = [ytdlp_path(), *ytdlp_extra_args(), "-t", "sleep", "-j", "--skip-download",
           "--no-warnings", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, **_NOWWIN)
    except (subprocess.TimeoutExpired, OSError):
        return {"video_id": video_id, "verdict": "unknown", "language": None}
    line = next((ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")),
                None)
    if not line:
        return {"video_id": video_id, "verdict": "unknown", "language": None}
    try:
        info = json.loads(line)
    except json.JSONDecodeError:
        return {"video_id": video_id, "verdict": "unknown", "language": None}
    auto = info.get("automatic_captions") or {}
    lang = (info.get("language") or "").lower()
    if lang == "ja" or "ja-orig" in auto:
        verdict = "ja"
    elif auto or lang:            # ASR heard speech, just not Japanese
        verdict = "non_ja"
    else:                          # no ASR track at all → nothing was said
        verdict = "silent"
    return {"video_id": video_id, "verdict": verdict, "language": lang or None}


def gate_speech(conn, video_ids, recheck=False):
    """Probe each candidate for Japanese speech and move the speechless ones
    (`silent`/`non_ja`) to status 'no_speech' so they can't be recommended.
    Verdicts are cached in meta.speech, so re-running is cheap and idempotent;
    pass recheck=True to re-probe. 'unknown' (probe failed) is left 'new' — never
    silently dropped. Returns the list of per-video verdict dicts."""
    out = []
    ts = now_iso()
    for vid in video_ids:
        row = conn.execute("SELECT status, meta FROM candidates WHERE video_id=?",
                           (vid,)).fetchone()
        if row is None:
            out.append({"video_id": vid, "verdict": "unknown",
                        "note": "not in pool"})
            continue
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except json.JSONDecodeError:
            meta = {}
        cached = meta.get("speech")
        if cached and not recheck:
            out.append({"video_id": vid, "status": row["status"], **cached,
                        "cached": True})
            continue
        v = probe_speech(vid)
        meta["speech"] = {"verdict": v["verdict"], "language": v["language"],
                          "checked": ts}
        # A failed probe stays 'new'; a real speech verdict decides the status.
        if v["verdict"] in ("silent", "non_ja") and row["status"] != "dismissed":
            new_status = "no_speech"
        elif v["verdict"] == "ja" and row["status"] == "no_speech":
            new_status = "new"          # recheck rescued a mis-gated one
        else:
            new_status = row["status"]
        conn.execute("UPDATE candidates SET status=?, meta=? WHERE video_id=?",
                     (new_status, json.dumps(meta, ensure_ascii=False), vid))
        out.append({"video_id": vid, "status": new_status, **meta["speech"]})
    conn.commit()
    return out


def refilter(conn, blocklist):
    """Re-apply the blocklist to the existing pool: any 'new' candidate that now
    matches gets moved to 'filtered'. Use after editing discover.format_blocklist.
    Returns the count moved."""
    fmt_subs, chan_ids = blocklist
    moved = 0
    for r in conn.execute("SELECT * FROM candidates WHERE status='new'").fetchall():
        if is_blocked(dict(r), fmt_subs, chan_ids):
            conn.execute("UPDATE candidates SET status='filtered' WHERE video_id=?",
                        (r["video_id"],))
            moved += 1
    conn.commit()
    return moved


def list_candidates(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE status=? ORDER BY first_seen DESC",
            (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM candidates ORDER BY first_seen DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
        except json.JSONDecodeError:
            d["meta"] = {}
        out.append(d)
    return out


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="verb", required=True)

    sub.add_parser("seeds", help="dump ledger-derived seeds + rated history (JSON)")

    p = sub.add_parser("run", help="harvest the given edges into discover.db")
    p.add_argument("--related", nargs="*", default=[], metavar="VIDEO_ID",
                   help="seed video ids → their related-video rails")
    p.add_argument("--search", nargs="*", default=[], metavar="QUERY",
                   help="search strings (quote each; JP query expansion goes here)")
    p.add_argument("--rss", nargs="*", default=[], metavar="CHANNEL_ID",
                   help="channel ids → their latest-uploads feed")
    p.add_argument("--search-n", type=int, default=8,
                   help="results per search query (default 8)")

    p = sub.add_parser("list", help="dump the candidate pool (JSON) for ranking")
    p.add_argument("--status", choices=STATUSES)

    sub.add_parser("refilter", help="re-apply discover.format_blocklist to the "
                   "existing pool (move newly-matching 'new' rows to 'filtered')")

    p = sub.add_parser("gate-speech", help="probe candidates for Japanese speech "
                       "(via yt-dlp); move silent / non-Japanese ones to 'no_speech'")
    p.add_argument("video_id", nargs="+", metavar="VIDEO_ID",
                   help="candidate ids to check (run on your ranked shortlist)")
    p.add_argument("--recheck", action="store_true",
                   help="re-probe even if a cached verdict exists")

    p = sub.add_parser("set-status", help="move a candidate's status")
    p.add_argument("video_id")
    p.add_argument("status", choices=STATUSES)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.verb == "seeds":
        print(json.dumps(gather_seeds(cfg), ensure_ascii=False, indent=2))
        return

    conn = open_discover(discover_db_path(cfg))

    if args.verb == "run":
        exclude = known_video_ids(cfg)
        blocklist = load_blocklist(cfg)
        harvested = []
        for vid in args.related:
            harvested += related(vid)
        for q in args.search:
            harvested += search(q, n=args.search_n)
        for cid in args.rss:
            harvested += channel_rss(cid)
        new_rows, n_filtered = store_candidates(conn, harvested, exclude, blocklist)
        print(f"harvested {len(harvested)} raw · {len(new_rows)} new · "
              f"{n_filtered} format-filtered (after ledger + dedup filter)",
              file=sys.stderr)
        print(json.dumps(new_rows, ensure_ascii=False, indent=2))

    elif args.verb == "refilter":
        moved = refilter(conn, load_blocklist(cfg))
        print(f"moved {moved} candidate(s) new → filtered", file=sys.stderr)
        print(json.dumps({"filtered": moved}, ensure_ascii=False))

    elif args.verb == "gate-speech":
        verdicts = gate_speech(conn, args.video_id, recheck=args.recheck)
        kept = sum(1 for v in verdicts if v.get("verdict") == "ja")
        dropped = sum(1 for v in verdicts if v.get("status") == "no_speech")
        print(f"speech-gated {len(verdicts)} · {kept} ja · {dropped} → no_speech",
              file=sys.stderr)
        print(json.dumps(verdicts, ensure_ascii=False, indent=2))

    elif args.verb == "list":
        print(json.dumps(list_candidates(conn, args.status), ensure_ascii=False, indent=2))

    elif args.verb == "set-status":
        cur = conn.execute("UPDATE candidates SET status=? WHERE video_id=?",
                           (args.status, args.video_id))
        conn.commit()
        if cur.rowcount == 0:
            ap.error(f"no candidate with video_id {args.video_id}")
        print(json.dumps({"video_id": args.video_id, "status": args.status},
                        ensure_ascii=False))


if __name__ == "__main__":
    main()
