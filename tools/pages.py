#!/usr/bin/env python3
"""pages — web page → readable post doc + sentence transcript (the Pages tab).

The text sibling of acquire: a queued page gets the same Stage-1 treatment as
a video (sentence track → coverage → any-word popup → ledger exposures) but
renders as readable posts on the phone instead of playing. Throwaway by
design: read → taps/known marks land in the ledger → delete purges the files
and keeps the evidence. No Anki cards, no recommender footprint.

Targets 5ch threads for now (itest.5ch.io / <server>.5ch.net|.io read.cgi
URLs). The itest frontend is a JS shell; the classic read.cgi HTML on the
board host carries the full thread as parseable Shift_JIS markup, so that is
what we fetch regardless of which URL form was shared.

Stages under <work_dir>/episodes/page_5ch_<board>_<thread>/:
    transcript.json   same shape acquire writes (sentences carry no timing —
                      start/end are 0.0), so coverage/definitions/transcript
                      endpoints work unchanged
    page.json         the post structure the phone reader renders: per post
                      n/name/date/uid/replies_to + lines as sentence-idx runs

CLI:
    python -m tools.pages <url> [--config PATH]
"""

import argparse
import html as HT
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._staging import episode_dir, write_json  # noqa: E402

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# https://itest.5ch.io/<server>/test/read.cgi/<board>/<thread>[/...]
_ITEST_RE = re.compile(
    r"^https?://itest\.5ch\.(?:net|io)/(?P<server>[a-z0-9]+)"
    r"/test/read\.cgi/(?P<board>[A-Za-z0-9_]+)/(?P<thread>\d+)")
# https://<server>.5ch.net/test/read.cgi/<board>/<thread>[/...]
_CLASSIC_RE = re.compile(
    r"^https?://(?P<server>[a-z0-9]+)\.5ch\.(?:net|io)"
    r"/test/read\.cgi/(?P<board>[A-Za-z0-9_]+)/(?P<thread>\d+)")

SENTENCE_ENDERS = "。！？…‼⁉"


def parse_5ch_url(url):
    """5ch thread URL (itest or classic, .net or .io) → {server, board,
    thread}, or None for anything that isn't one."""
    url = (url or "").strip()
    m = _ITEST_RE.match(url) or _CLASSIC_RE.match(url)
    if not m or m["server"] == "itest":
        return None
    return {"server": m["server"], "board": m["board"], "thread": m["thread"]}


def is_page_source(source):
    return parse_5ch_url(source) is not None


def page_episode_id(source):
    """Stable id derivable from the URL alone (jobqueue.derive_job_id calls
    this pre-network, the same way yt_<vid> works). The page_ prefix is what
    marks a job/episode as a page everywhere downstream."""
    ref = parse_5ch_url(source)
    if not ref:
        return None
    return f"page_5ch_{ref['board']}_{ref['thread']}"


def canonical_url(ref):
    return (f"https://{ref['server']}.5ch.net/test/read.cgi/"
            f"{ref['board']}/{ref['thread']}/")


def fetch_thread(ref, log=print):
    """GET the classic read.cgi HTML (follows the .net → .io redirect) and
    decode it — Shift_JIS unless the page head says otherwise."""
    import requests
    url = canonical_url(ref)
    log(f"fetching {url}")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    head = r.content[:2048].decode("ascii", errors="ignore").lower()
    enc = "utf-8" if "utf-8" in head else "cp932"
    return r.content.decode(enc, errors="replace")


_POST_RE = re.compile(
    r'<div id="(?P<n>\d+)" [^>]*data-userid="(?P<uid>[^"]*)"[^>]*class="clear post">'
    r'.*?<span class="postusername">(?P<name>.*?)</span>'
    r'.*?<span class="date">(?P<date>[^<]*)</span>'
    r'.*?<div class="post-content">(?P<content>.*?)</div></div>',
    re.S)


def _clean_body(content):
    """post-content markup → plain text with \\n line breaks. Reply anchors
    become bare >>N (the reader re-linkifies them); other anchors keep just
    their href; remaining tags drop."""
    c = re.sub(r'<a [^>]*class="reply_link"[^>]*>&gt;&gt;(\d+)</a>', r'>>\1', content)
    c = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>', r'\1', c)
    c = re.sub(r'<br\s*/?>', '\n', c)
    c = re.sub(r'<[^>]+>', '', c)
    c = HT.unescape(c)
    return '\n'.join(line.strip() for line in c.split('\n')).strip()


def parse_thread(html):
    """Classic read.cgi HTML → {title, posts:[{n,name,date,uid,body,
    replies_to}]}. body keeps the post's own line breaks."""
    m = re.search(r'<h1 id="threadtitle">(.*?)</h1>', html, re.S)
    if not m:
        m = re.search(r'<title>(.*?)</title>', html, re.S)
    title = HT.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else ""

    posts = []
    for pm in _POST_RE.finditer(html):
        body = _clean_body(pm["content"])
        name = HT.unescape(re.sub(r'<[^>]+>', '', pm["name"])).strip()
        posts.append({
            "n": int(pm["n"]),
            "name": name,
            "date": pm["date"].strip(),
            "uid": pm["uid"].removeprefix("ID:"),
            "body": body,
            "replies_to": sorted({int(x) for x in re.findall(r'>>(\d+)', body)}),
        })
    if not posts:
        raise RuntimeError("no posts found — 5ch markup changed or thread is gone")
    return {"title": title, "posts": posts}


def split_sentences(line):
    """One post line → sentence-sized chunks for coverage classification.
    Splits after sentence enders; a line without any stays whole (AA, URLs,
    fragments — all still tokenize fine)."""
    parts = re.split(f"(?<=[{SENTENCE_ENDERS}])", line)
    return [p.strip() for p in parts if p.strip()]


def acquire_page(source, cfg, log=print):
    """Fetch + parse one page source and stage its artifacts. Returns the
    transcript record (same contract as tools.acquire.acquire)."""
    ref = parse_5ch_url(source)
    if not ref:
        raise RuntimeError(f"not a supported page URL: {source}")
    episode_id = page_episode_id(source)

    thread = parse_thread(fetch_thread(ref, log=log))
    log(f"parsed {len(thread['posts'])} posts")

    sentences = []   # transcript track: flat, idx-ordered
    page_posts = []  # reader structure: lines as runs of sentence idxs
    for post in thread["posts"]:
        lines = []
        for line in post["body"].split("\n"):
            idxs = []
            for chunk in split_sentences(line):
                idxs.append(len(sentences))
                sentences.append(chunk)
            lines.append(idxs)  # empty run = blank line (paragraph break)
        page_posts.append({k: post[k] for k in
                           ("n", "name", "date", "uid", "replies_to")}
                          | {"lines": lines})

    episode = {
        "id": episode_id,
        "title": thread["title"] or f"5ch {ref['board']}/{ref['thread']}",
        "uploader": f"5ch/{ref['board']}",
        "source": canonical_url(ref),
        "kind": "page",
    }
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "episode": episode,
        "acquired_at": fetched_at,
        "punctuation_restored": False,
        # no timing — pages don't play. 0.0 keeps every consumer of
        # sentence start/end (coverage, /transcript) working unchanged.
        "sentences": [{"idx": i, "start": 0.0, "end": 0.0, "text": t}
                      for i, t in enumerate(sentences)],
    }

    ep_dir = episode_dir(cfg, episode_id, create=True)
    write_json(ep_dir / "transcript.json", record)
    write_json(ep_dir / "page.json", {
        "episode_id": episode_id,
        "title": episode["title"],
        "url": episode["source"],
        "site": "5ch",
        "board": ref["board"],
        "thread": ref["thread"],
        "fetched_at": fetched_at,
        "post_count": len(page_posts),
        "posts": page_posts,
    })
    log(f"staged {len(page_posts)} posts / {len(sentences)} sentences → {ep_dir}")
    return record


def main(argv=None):
    from lib_config import load_config
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="5ch thread URL (itest or classic)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    record = acquire_page(args.source, cfg, log=lambda m: print(m, file=sys.stderr))
    print(record["episode"]["id"])


if __name__ == "__main__":
    main()
