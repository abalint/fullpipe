#!/usr/bin/env python3
"""manga — manga volumes from the PC library → OCR'd, tokenized, readable on the phone.

The reading sibling of tools.series: the page scans live on the Windows
desktop (H:/manga/<Series>/<VOL n>/NNN.jpg) and are never written to. A
queued volume gets the same Stage-1 treatment as a video — text track →
coverage → any-word popup → ledger exposures — but the "transcript" is what
the desktop GPU reads off the pages (mokuro: comic-text-detector for the
speech-bubble boxes, manga-ocr for the text; gpu_service/ocr_volume.py), and
the phone renders it as an invisible, tappable, colour-washed layer over the
original page in the manga reader instead of playing anything.

Identity: source `manga://<slug>/<vol_no>` → episode id `manga_<slug>_v<nn>`
(the manga_ prefix is the job's kind marker everywhere downstream, like
page_). Rows carry `series` / `series_title` / `ep_no` so the phone groups
volumes under one header in order, the way box sets group.

Stages under <work_dir>/episodes/manga_<slug>_v<nn>/:
    pages/NNN.jpg     the page scans, pulled from the PC as-is (~300 kB each)
    ocr/NNN.json      mokuro's raw per-page result (kept for rebuilds)
    transcript.json   the sentence track — one sentence per speech-bubble
                      sentence, in reading order (right-to-left, top-to-
                      bottom); start/end are PSEUDO-TIMES: page index ×
                      PAGE_SECS, so "the line played" (exposure credit from a
                      sitting's played ranges) becomes "the page was viewed"
                      with no ledger changes
    manga.json        the reader structure: per page file/size + text blocks
                      (box, vertical, font size, per-line char counts) as
                      runs of sentence idxs into the transcript

CLI:
    python -m tools.manga library                              # what's on the PC (H:/manga)
    python -m tools.manga scan   "H:/manga/Dandadan"           # volumes that would be ingested
    python -m tools.manga ingest "H:/manga/Dandadan" [--slug S] [--title T] [--volumes 1,3-5]
                                                     [--dry-run] [--no-drain]
    python -m tools.manga list
    python -m tools.manga status dandadan
    python -m tools.manga remove dandadan [--remote]           # Mac (+ PC OCR cache); never the scans
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._staging import episode_dir, read_json, write_json  # noqa: E402
from tools.series import Remote, _win, _wname, series_cfg, slugify  # noqa: E402

SCHEME = "manga://"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# pseudo-seconds per page: sentence.start = page_idx * PAGE_SECS. 30 s is a
# plausible reading pace, so a sitting's wall-clock seconds over the
# volume's "duration" (pages × 30) reads as a sane play fraction too.
PAGE_SECS = 30.0
SENTENCE_ENDERS = "。！？…‼⁉"
MIN_PAGES = 3  # a folder with fewer images is a cover/extras dir, not a volume
DEFAULTS = {
    "remote_root": "H:/manga",
    "remote_ocr_dir": "I:/transcribe/fullpipe_manga",
    "remote_python": "I:/transcribe/mokuro/.venv/Scripts/python.exe",
    "remote_script": "I:/transcribe/ocr_volume.py",
}


# --- identity ------------------------------------------------------------------

def manga_source(slug, vol_no):
    return f"{SCHEME}{slug}/{int(vol_no)}"


def parse_manga_source(source):
    """'manga://dandadan/3' → ('dandadan', 3); None for anything else."""
    m = re.match(r"^manga://([a-z0-9][a-z0-9-]*)/(\d+)$", str(source or "").strip())
    return (m.group(1), int(m.group(2))) if m else None


def is_manga_source(source):
    return parse_manga_source(source) is not None


def episode_id_for(slug, vol_no):
    return f"manga_{slug}_v{int(vol_no):02d}"


def manga_episode_id(source):
    parsed = parse_manga_source(source)
    return episode_id_for(*parsed) if parsed else None


def is_manga_episode(episode_id):
    return str(episode_id or "").startswith("manga_")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- config / manifests ----------------------------------------------------------

def manga_cfg(cfg):
    """ssh settings come from the series block (same desktop); the manga
    block adds the library root + the OCR venv/script/cache on the PC."""
    return {**series_cfg(cfg), **DEFAULTS, **(cfg.get("manga") or {})}


def manga_root(cfg):
    return Path(cfg["work_dir"]).expanduser() / "manga"


def manga_dir(cfg, slug, create=False):
    d = manga_root(cfg) / slug
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(cfg, slug):
    return manga_dir(cfg, slug) / "manga.json"


def load_manifest(cfg, slug):
    p = manifest_path(cfg, slug)
    if not p.exists():
        raise FileNotFoundError(f"no manga '{slug}' under {manga_root(cfg)}")
    return read_json(p)


def save_manifest(cfg, man):
    write_json(manifest_path(cfg, man["slug"]), man)


def list_manga(cfg):
    root = manga_root(cfg)
    if not root.exists():
        return []
    return [read_json(p) for p in sorted(root.glob("*/manga.json"))]


def find_volume(man, vol_no):
    for v in man["volumes"]:
        if v["vol_no"] == int(vol_no):
            return v
    raise KeyError(f"volume {vol_no} not in manga '{man['slug']}'")


def remote_ocr_dir(mcfg, slug, vol_no):
    return f"{mcfg['remote_ocr_dir']}/{slug}/v{int(vol_no):02d}"


# --- folder names ----------------------------------------------------------------

_VOL_PATTERNS = (
    re.compile(r"(?i)\bvol(?:ume)?\.?\s*_?(\d{1,3})"),
    re.compile(r"第\s*(\d{1,3})\s*巻"),
    re.compile(r"(\d{1,3})\s*巻"),
    re.compile(r"(?i)\bv(\d{1,3})\b"),
    re.compile(r"(?i)\bch(?:apter)?\.?\s*_?(\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,3})(?!\d)"),
)


def parse_volume(name):
    """Folder name → volume number: 'VOL 1 (JA)', 'Vol.01', 'v01', '第3巻',
    'Chapter 12', or the first bare 1–3 digit number. None when nothing parses."""
    for pat in _VOL_PATTERNS:
        m = pat.search(str(name))
        if m:
            return int(m.group(1))
    return None


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(name))]


def _wsuffix(p):
    name = _wname(p)
    return ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""


def _wparent(p):
    return str(p)[: len(str(p)) - len(_wname(p))].rstrip("\\/")


def group_volumes(paths, remote_dir):
    """PC file listing under a series folder → volumes: every folder holding
    at least MIN_PAGES page images, numbered from its name (relative to the
    series folder). A series folder that holds the pages itself is one
    volume. Returns (volumes sorted by vol_no, folders that didn't parse)."""
    by_dir = {}
    for p in paths:
        if _wsuffix(p) in IMAGE_EXTS and not _wname(p).startswith("."):
            by_dir.setdefault(_wparent(p), []).append(_wname(p))
    root = str(remote_dir).replace("/", "\\").rstrip("\\")
    volumes, unparsed = [], []
    for d, files in by_dir.items():
        if len(files) < MIN_PAGES:
            continue
        rel = d.replace("/", "\\")
        rel = rel[len(root):].strip("\\") if rel.lower().startswith(root.lower()) else _wname(d)
        if not rel:  # the series folder itself is the volume
            vol_no, label = 1, _wname(root)
        else:
            vol_no, label = parse_volume(rel), rel
        if vol_no is None:
            unparsed.append(d)
            continue
        volumes.append({"vol_no": vol_no, "label": label, "remote_dir": d,
                        "pages": len(files)})
    volumes.sort(key=lambda v: (v["vol_no"], natural_key(v["label"])))
    seen, out = set(), []
    for v in volumes:
        if v["vol_no"] in seen:  # two folders claiming the same number: keep the first
            unparsed.append(v["remote_dir"])
            continue
        seen.add(v["vol_no"])
        out.append(v)
    return out, unparsed


def scan(cfg, remote_dir, remote=None, log=print):
    remote = remote or Remote(manga_cfg(cfg))
    paths = remote.listing(remote_dir)
    volumes, unparsed = group_volumes(paths, remote_dir)
    log(f"{len(paths)} files under {remote_dir}: {len(volumes)} volume(s)")
    return volumes, unparsed


def library(cfg, remote=None):
    """Every series folder under the PC's manga root with its volumes —
    what the phone's library picker and `/manga library` show. One
    recursive listing (a few seconds for ~50k files)."""
    mcfg = manga_cfg(cfg)
    remote = remote or Remote(mcfg)
    root = str(mcfg["remote_root"]).replace("/", "\\").rstrip("\\")
    paths = remote.listing(root)
    by_series = {}
    for p in paths:
        rel = str(p).replace("/", "\\")
        if not rel.lower().startswith(root.lower() + "\\"):
            continue
        head = rel[len(root) + 1:].split("\\", 1)[0]
        by_series.setdefault(head, []).append(p)
    out = []
    for name, files in sorted(by_series.items(), key=lambda kv: natural_key(kv[0])):
        sdir = f"{root}\\{name}"
        if _wsuffix(name) in IMAGE_EXTS:  # a loose image at the root, not a series
            continue
        volumes, _ = group_volumes(files, sdir)
        if not volumes:
            continue
        out.append({"name": name, "slug": slugify(name),
                    "remote_dir": sdir.replace("\\", "/"),
                    "volumes": [{k: v[k] for k in ("vol_no", "label", "pages")}
                                for v in volumes]})
    return out


# --- OCR blocks → sentence track ---------------------------------------------------

def _overlap(a1, a2, b1, b2):
    return min(a2, b2) - max(a1, b1)


def _chain(items, lo, hi, key):
    """Group items whose [lo, hi] spans overlap transitively (tolerance: a
    quarter of the smaller span), in order of `key`."""
    groups = []
    for it in sorted(items, key=key):
        a1, a2 = lo(it), hi(it)
        for g in groups:
            b1, b2 = g["lo"], g["hi"]
            tol = 0.25 * min(a2 - a1, b2 - b1)
            if _overlap(a1, a2, b1, b2) > tol:
                g["items"].append(it)
                g["lo"], g["hi"] = min(b1, a1), max(b2, a2)
                break
        else:
            groups.append({"lo": a1, "hi": a2, "items": [it]})
    return groups


def sort_blocks(blocks):
    """Manga reading order without panel detection: tiers by vertical
    overlap (top → bottom), columns of horizontal overlap within a tier
    (right → left), top → bottom within a column."""
    tiers = _chain(blocks, lo=lambda b: b["box"][1], hi=lambda b: b["box"][3],
                   key=lambda b: b["box"][1])
    tiers.sort(key=lambda g: g["lo"])
    ordered = []
    for tier in tiers:
        cols = _chain(tier["items"], lo=lambda b: b["box"][0], hi=lambda b: b["box"][2],
                      key=lambda b: -b["box"][2])
        cols.sort(key=lambda g: -g["hi"])
        for col in cols:
            ordered.extend(sorted(col["items"], key=lambda b: (b["box"][1], -b["box"][2])))
    return ordered


def split_sentences(text):
    """One bubble's text → sentence chunks, splitting after enders. No
    stripping: the reader walks characters against per-line counts, so the
    chunks must concatenate back to the block text exactly."""
    parts = re.split(f"(?<=[{SENTENCE_ENDERS}])", text)
    return [p for p in parts if p]


def clean_line(line):
    """OCR line → the characters the reader lays out (no whitespace: vertical
    text has none, and a stray space would desync the per-line counts)."""
    return re.sub(r"\s+", "", str(line or ""))


_JAPANESE = re.compile(r"[ぁ-ゖァ-ヶー㐀-鿿々〆]")


def is_dialogue(text):
    """A text block worth a sentence: it has Japanese in it. Scan-site
    watermarks (ｍａｎｇｏ「ｅｏｄｅｒ．ｔｏ), page numbers and Latin-only
    debris OCR as real blocks and would pollute the transcript otherwise."""
    return bool(_JAPANESE.search(text))


def page_from_ocr(page_no, file, ocr, sentences, read=None, glosses=None):
    """One mokuro page result → the reader's page entry, appending its
    sentences (reading order) to the transcript track.

    `read` is the AI read of this page (read_apply): {raw block index →
    {"lines": [...], "gloss": str|None}}. Where present its lines replace
    mokuro's draft (an empty list drops the block — a watermark, a logo),
    and its gloss lands on every sentence of the bubble via `glosses`
    ({sentence idx → gloss}).

    Geometry: the block box, and — when mokuro's per-line polygons
    (`lines_coords`) pair one-to-one with the lines kept — `line_boxes`,
    one [x1, y1, x2, y2] per printed line, so the phone lays each line on
    the glyphs it was read from instead of sharing the bubble box evenly
    (the read keeps the printed line breaks, so the pairing holds on
    nearly every bubble; where the counts differ the bubble box alone
    ships and the reader falls back)."""
    w, h = int(ocr.get("img_width") or 0), int(ocr.get("img_height") or 0)
    blocks = []
    raw = ocr.get("blocks") or []
    order = sort_blocks([{**b, "_k": k} for k, b in enumerate(raw)])
    for k, b in enumerate(order):
        got = (read or {}).get(str(b["_k"]))
        src_lines = got["lines"] if got is not None else (b.get("lines") or [])
        coords = b.get("lines_coords") or []
        if len(coords) != len(src_lines):
            coords = [None] * len(src_lines)
        kept = [(clean_line(l), c) for l, c in zip(src_lines, coords)]
        kept = [(l, c) for l, c in kept if l]
        lines = [l for l, _ in kept]
        line_boxes = [line_box(c) for _, c in kept]
        text = "".join(lines)
        if not text or not is_dialogue(text):
            continue
        start = round(page_no * PAGE_SECS + min(k * 0.1, PAGE_SECS - 1.0), 1)
        sents = []
        for chunk in split_sentences(text):
            sents.append(len(sentences))
            sentences.append({"idx": len(sentences), "start": start,
                              "end": round(start + 0.1, 1), "text": chunk})
        gloss = (got or {}).get("gloss") if got else None
        if gloss and glosses is not None:
            for si in sents:
                glosses[si] = str(gloss).strip()
        box = [round(float(v), 1) for v in b["box"]]
        block = {"box": box, "vertical": bool(b.get("vertical", True)),
                 "font_size": round(float(b.get("font_size") or 0), 1),
                 "lines": [len(l) for l in lines], "sents": sents, "k": b["_k"]}
        if line_boxes and all(line_boxes):
            block["line_boxes"] = line_boxes
        blocks.append(block)
    return {"n": page_no, "file": file, "w": w, "h": h, "blocks": blocks}


def line_box(poly):
    """A mokuro line polygon (4 points, any order) → its axis-aligned box
    [x1, y1, x2, y2]; None for a missing / malformed polygon."""
    try:
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
    except (TypeError, IndexError, ValueError):
        return None
    if not xs or not ys:
        return None
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def rebuild(cfg, episode_id, log=print):
    """Re-emit manga.json + transcript.json from the OCR + read already on
    disk, without touching coverage — for a structure change (new block
    fields) on a volume already read and covered. Refuses if the sentence
    track would differ from the one coverage.json indexes: the reader
    joins the two by idx."""
    ep_dir = episode_dir(cfg, episode_id)
    doc = read_json(ep_dir / "manga.json")
    before = read_json(ep_dir / "transcript.json")["sentences"]
    meta = {"slug": doc["slug"], "vol_no": doc["vol_no"], "label": doc.get("label"),
            "title": doc["title"], "series_title": doc["series_title"]}
    files = [p["file"] for p in doc["pages"]]
    sentences, pages, glosses = [], [], {}
    read = load_read(ep_dir / "ocr")
    for n, file in enumerate(files):
        p = ep_dir / "ocr" / (Path(file).stem + ".json")
        ocr = read_json(p) if p.exists() else {"blocks": []}
        pages.append(page_from_ocr(n, file, ocr, sentences,
                                   read=read.get(Path(file).stem), glosses=glosses))
    if [s["text"] for s in sentences] != [s["text"] for s in before]:
        raise RuntimeError("sentence track would change — run read-apply (re-runs coverage)")
    doc["pages"] = pages
    doc["page_count"] = len(pages)
    doc["built_at"] = now_iso()
    write_json(ep_dir / "manga.json", doc)
    lined = sum(1 for pg in pages for b in pg["blocks"] if b.get("line_boxes"))
    n_blocks = sum(len(pg["blocks"]) for pg in pages)
    log(f"rebuilt {len(pages)} pages / {n_blocks} bubbles ({lined} with line boxes) → {ep_dir}")
    return {"pages": len(pages), "blocks": n_blocks, "lined": lined}


def load_read(ocr_dir):
    """The AI read (read_apply) — {page stem: {raw block index: {lines,
    gloss}}} from ocr/read/<stem>.json, {} when no agent has read yet."""
    out = {}
    for p in sorted((Path(ocr_dir) / "read").glob("*.json")):
        if p.name in ("manifest.json", "applied.json"):
            continue
        try:
            d = read_json(p)
        except ValueError:
            continue
        blocks = d.get("blocks")
        if isinstance(blocks, dict):
            out[p.stem] = {str(k): v for k, v in blocks.items()
                           if isinstance(v, dict) and isinstance(v.get("lines"), list)}
    return out


def build_volume(cfg, episode_id, meta, ocr_dir, page_files, log=print):
    """ocr/*.json (+ ocr/read/*.json where agents have read) + the page list
    → transcript.json + manga.json (+ read/lines.json: bubble glosses by
    sentence idx) under the episode dir. Returns the transcript record
    (tools.acquire contract)."""
    sentences, pages, glosses = [], [], {}
    read = load_read(ocr_dir)
    for n, file in enumerate(page_files):
        p = Path(ocr_dir) / (Path(file).stem + ".json")
        ocr = read_json(p) if p.exists() else {"blocks": []}
        pages.append(page_from_ocr(n, file, ocr, sentences,
                                   read=read.get(Path(file).stem), glosses=glosses))
    episode = {
        "id": episode_id,
        "title": meta["title"],
        "uploader": meta["series_title"],  # taste grouping = the manga, like a series
        "source": manga_source(meta["slug"], meta["vol_no"]),
        "kind": "manga",
    }
    record = {
        "episode": episode,
        "acquired_at": now_iso(),
        "punctuation_restored": False,
        "sentences": sentences,
    }
    ep_dir = episode_dir(cfg, episode_id, create=True)
    write_json(ep_dir / "transcript.json", record)
    write_json(ep_dir / "manga.json", {
        "episode_id": episode_id,
        "slug": meta["slug"],
        "title": meta["title"],
        "series_title": meta["series_title"],
        "vol_no": meta["vol_no"],
        "label": meta.get("label"),
        "reading": "rtl",
        "page_secs": PAGE_SECS,
        "page_count": len(pages),
        "read_by": "opus" if read else "mokuro",
        "pages_read": len(read),
        "built_at": record["acquired_at"],
        "pages": pages,
    })
    if glosses:
        write_json(ep_dir / "read" / "lines.json",
                   {"episode_id": episode_id, "lines": [
                       {"idx": i, "gloss": g} for i, g in sorted(glosses.items())]})
    n_blocks = sum(len(p["blocks"]) for p in pages)
    log(f"built {len(pages)} pages / {n_blocks} bubbles / {len(sentences)} sentences"
        f" ({'AI read' if read else 'mokuro draft'}, {len(glosses)} glossed) → {ep_dir}")
    return record


# --- the PC: OCR + pull ---------------------------------------------------------------

def remote_ocr(remote, mcfg, src_dir, out_dir, log=print, timeout=None):
    """Run gpu_service/ocr_volume.py on the desktop, streaming its progress
    lines to `log` (the worker narrates them on the queue row). Idempotent:
    the script skips pages already done and a finished volume is a no-op."""
    # cmd.exe `set` keeps a trailing space in the value — the quoted form doesn't
    hf_home = _win(mcfg.get("remote_hf_home") or "I:/transcribe/hf_cache")
    cmd = (f'set "PYTHONIOENCODING=utf-8" & set "HF_HOME={hf_home}" & '
           f'"{_win(mcfg["remote_python"])}" "{_win(mcfg["remote_script"])}" '
           f'"{_win(src_dir)}" "{_win(out_dir)}"')
    p = subprocess.Popen([*remote._base("ssh"), remote.host, cmd],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail = []
    for raw in p.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line or line.startswith("**"):
            continue
        tail.append(line)
        del tail[:-20]
        log(f"ocr: {line}")
    p.wait(timeout=timeout or remote.timeout)
    if p.returncode != 0:
        raise RuntimeError("remote OCR failed:\n" + "\n".join(tail))


def pull_tree(remote, remote_dir, local_dir, log=print):
    """Copy a PC folder to the Mac in one ssh stream (Windows' bsdtar →
    local tar) — one connection for 200 page files instead of 200 scp
    handshakes. Falls back to scp -r when tar isn't available remotely."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    log(f"pulling {remote_dir}")
    src = subprocess.Popen([*remote._base("ssh"), remote.host,
                            f'tar -cf - -C "{_win(remote_dir)}" .'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen(["tar", "-xf", "-", "-C", str(local_dir)],
                           stdin=src.stdout, stderr=subprocess.PIPE)
    src.stdout.close()
    _, dst_err = dst.communicate(timeout=remote.timeout)
    src_err = src.communicate(timeout=60)[1]
    if src.returncode == 0 and dst.returncode == 0:
        return local_dir
    err = (src_err + dst_err).decode("utf-8", "replace")
    log(f"tar stream failed ({src.returncode}/{dst.returncode}) — falling back to scp")
    r = subprocess.run([*remote._base("scp"), "-q", "-r",
                        f"{remote.host}:{remote_dir}/.", str(local_dir)],
                       capture_output=True, timeout=remote.timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pull failed: {err.strip()[-300:]} / "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return local_dir


def page_files_in(local_dir):
    local_dir = Path(local_dir)
    if not local_dir.is_dir():
        return []
    return sorted((p.name for p in local_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                   and not p.name.startswith(".")), key=natural_key)


def acquire_manga(source, cfg, log=print, remote=None):
    """Stage 1's acquire for a manga volume: OCR on the PC (skipped when its
    cache says done) → pull pages + OCR json → build the sentence track.
    Returns the transcript record (same contract as tools.acquire.acquire)."""
    parsed = parse_manga_source(source)
    if not parsed:
        raise RuntimeError(f"not a manga source: {source}")
    slug, vol_no = parsed
    man = load_manifest(cfg, slug)
    vol = find_volume(man, vol_no)
    episode_id = episode_id_for(slug, vol_no)
    mcfg = manga_cfg(cfg)
    remote = remote or Remote(mcfg)
    ep_dir = episode_dir(cfg, episode_id, create=True)
    pages_dir, ocr_dir = ep_dir / "pages", ep_dir / "ocr"

    out_dir = remote_ocr_dir(mcfg, slug, vol_no)
    if not (ocr_dir / "_done.json").exists():
        log(f"OCR {vol['label']} on the PC ({vol.get('pages', '?')} pages)")
        remote_ocr(remote, mcfg, vol["remote_dir"], out_dir, log=log)
        pull_tree(remote, out_dir, ocr_dir, log=log)
    if not (ocr_dir / "_done.json").exists():
        raise RuntimeError("OCR finished without a _done.json — check the PC log")
    done = read_json(ocr_dir / "_done.json")
    if len(page_files_in(pages_dir)) < len(done["pages"]):
        pull_tree(remote, vol["remote_dir"], pages_dir, log=log)
    # ._NNN.jpg AppleDouble twins (scans copied from a Mac) OCR as junk on
    # the PC and never extract here — they are not pages
    files = [f for f in done["pages"]
             if not f.startswith(".") and (pages_dir / f).exists()]
    if not files:
        raise RuntimeError(f"no page images landed under {pages_dir}")
    meta = {"slug": slug, "vol_no": vol_no, "label": vol["label"],
            "title": vol["title"], "series_title": man["title"]}
    record = build_volume(cfg, episode_id, meta, ocr_dir, files, log=log)
    vol["page_count"] = len(files)
    vol["ocr_elapsed"] = done.get("elapsed")
    vol["pulled_at"] = now_iso()
    save_manifest(cfg, man)
    return record


# --- the AI read: Opus agents read the pages mokuro boxed -----------------------------

LABEL_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def annotate_page(image_path, blocks, out_path):
    """The page with every mokuro box outlined and numbered (raw block
    index) — what a reading agent looks at. Numbers sit just outside the
    box's top-right corner so they never cover the text."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype(LABEL_FONT, 22)
    except OSError:
        font = ImageFont.load_default()
    for k, b in enumerate(blocks):
        x1, y1, x2, y2 = [float(v) for v in b["box"]]
        draw.rectangle([x1, y1, x2, y2], outline=(230, 30, 30), width=2)
        label = str(k)
        tw = draw.textlength(label, font=font)
        lx = min(im.width - tw - 6, x2 + 3)
        ly = max(0, y1 - 26)
        draw.rectangle([lx - 3, ly, lx + tw + 3, ly + 24], fill=(230, 30, 30))
        draw.text((lx, ly), label, fill="white", font=font)
    im.save(out_path, "PNG", optimize=True)


def read_prep(cfg, episode_id, log=print):
    """Stage the AI read: ocr/read/pages/<stem>.png (numbered boxes) and
    ocr/read/manifest.json — per page: file, raw blocks (index, box,
    vertical, mokuro's draft lines) — for the /manga skill's agents.
    Pages already read (ocr/read/<stem>.json) are listed as done."""
    ep_dir = episode_dir(cfg, episode_id)
    ocr_dir, pages_dir = ep_dir / "ocr", ep_dir / "pages"
    read_dir = ocr_dir / "read"
    (read_dir / "pages").mkdir(parents=True, exist_ok=True)
    files = page_files_in(pages_dir)
    if not files:
        raise RuntimeError(f"no pages under {pages_dir} — run Stage 1 first")
    manifest = {"episode_id": episode_id, "pages": []}
    for n, file in enumerate(files):
        stem = Path(file).stem
        p = ocr_dir / f"{stem}.json"
        ocr = read_json(p) if p.exists() else {"blocks": []}
        raw = ocr.get("blocks") or []
        png = read_dir / "pages" / f"{stem}.png"
        if raw and not png.exists():
            annotate_page(pages_dir / file, raw, png)
        manifest["pages"].append({
            "n": n, "file": file, "stem": stem,
            "image": str(png) if raw else None,
            "done": (read_dir / f"{stem}.json").exists(),
            "blocks": [{"k": k, "box": [round(float(v)) for v in b["box"]],
                        "vertical": bool(b.get("vertical", True)),
                        "draft": [clean_line(l) for l in (b.get("lines") or [])]}
                       for k, b in enumerate(raw)],
        })
    write_json(read_dir / "manifest.json", manifest)
    todo = [p for p in manifest["pages"] if p["blocks"] and not p["done"]]
    log(f"read prep: {len(files)} pages, {len(todo)} to read, "
        f"{sum(len(p['blocks']) for p in todo)} boxes → {read_dir}")
    return manifest


def read_status(cfg, episode_id):
    ep_dir = episode_dir(cfg, episode_id)
    read_dir = ep_dir / "ocr" / "read"
    files = page_files_in(ep_dir / "pages")
    with_boxes = 0
    for f in files:
        p = ep_dir / "ocr" / f"{Path(f).stem}.json"
        if p.exists() and (read_json(p).get("blocks") or []):
            with_boxes += 1
    done = [f for f in files if (read_dir / f"{Path(f).stem}.json").exists()]
    return {"episode_id": episode_id, "pages": len(files), "pages_with_text": with_boxes,
            "read": len(done), "applied": (read_dir / "applied.json").exists()}


def read_apply(cfg, episode_id, log=print, min_fraction=0.95):
    """Rebuild the volume from the agents' reads (ocr/read/<stem>.json) and
    re-run coverage. Refuses while fewer than min_fraction of the pages
    with text are read — a half-read volume would mix draft and read
    sentences and shift every idx later."""
    st = read_status(cfg, episode_id)
    if st["pages_with_text"] and st["read"] < st["pages_with_text"] * min_fraction:
        raise RuntimeError(f"only {st['read']}/{st['pages_with_text']} pages read — "
                           "run the agents over the rest first")
    ep_dir = episode_dir(cfg, episode_id)
    doc = read_json(ep_dir / "manga.json")
    meta = {"slug": doc["slug"], "vol_no": doc["vol_no"], "label": doc.get("label"),
            "title": doc["title"], "series_title": doc["series_title"]}
    files = [p["file"] for p in doc["pages"]]
    record = build_volume(cfg, episode_id, meta, ep_dir / "ocr", files, log=log)
    from tools.coverage import run_coverage
    run_coverage(cfg, episode_id)
    write_json(ep_dir / "ocr" / "read" / "applied.json",
               {"applied_at": now_iso(), "pages_read": st["read"],
                "sentences": len(record["sentences"])})
    log(f"applied: {st['read']} pages read, coverage re-run")
    return st


# --- ingest / status / remove -------------------------------------------------------

def parse_volume_spec(spec):
    """'1,3-5' → {1,3,4,5}; None/'' → None (all)."""
    if not spec:
        return None
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def ingest(cfg, remote_dir, slug=None, title=None, volumes=None, dry_run=False,
           log=print, remote=None):
    """Scan the PC folder → manifest → enqueue one job per volume. The heavy
    work (OCR, pull, coverage) is Stage 1 — the server's worker (or a local
    drain) runs it, so the phone can queue volumes from the library picker
    the same way. Idempotent: re-runs add volumes, keep existing rows."""
    title = title or _wname(str(remote_dir).rstrip("/\\"))
    slug = slug or slugify(title)
    found, unparsed = scan(cfg, remote_dir, remote=remote, log=log)
    if not found:
        raise RuntimeError(f"no volumes with page images under {remote_dir}"
                           + (f" (unparsed: {unparsed[:5]})" if unparsed else ""))
    wanted = parse_volume_spec(volumes)
    picked = [v for v in found if wanted is None or v["vol_no"] in wanted]
    for v in picked:
        log(f"  vol {v['vol_no']:>3}  {v['label']}  {v['pages']} pages")
    if unparsed:
        log(f"  ignored (no volume number): {len(unparsed)} folder(s)")
    if dry_run:
        return {"slug": slug, "title": title, "volumes": picked, "unparsed": unparsed}

    try:
        man = load_manifest(cfg, slug)
    except FileNotFoundError:
        man = {"slug": slug, "title": title, "remote_dir": str(remote_dir),
               "created_at": now_iso(), "volumes": []}
    by_no = {v["vol_no"]: v for v in man["volumes"]}
    for v in picked:
        row = by_no.setdefault(v["vol_no"], {})
        row.update({"vol_no": v["vol_no"], "label": v["label"],
                    "id": episode_id_for(slug, v["vol_no"]),
                    "title": f"{man['title']} Vol. {v['vol_no']}",
                    "remote_dir": v["remote_dir"], "pages": v["pages"]})
    man["volumes"] = sorted(by_no.values(), key=lambda r: r["vol_no"])
    save_manifest(cfg, man)

    from server import jobqueue as q
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    summary = {"slug": slug, "title": man["title"], "enqueued": [], "already": []}
    for v in picked:
        row = by_no[v["vol_no"]]
        job, created = q.enqueue(conn, manga_source(slug, v["vol_no"]),
                                 title=row["title"], series=slug,
                                 series_title=man["title"], ep_no=v["vol_no"])
        (summary["enqueued"] if created or job["state"] == "queued"
         else summary["already"]).append(job["id"])
        log(f"  vol {v['vol_no']}: {'queued' if created else job['state']} ({job['id']})")
    return summary


def status(cfg, slug):
    from server import jobqueue as q
    man = load_manifest(cfg, slug)
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    rows = []
    for v in man["volumes"]:
        job = q.get_job(conn, v["id"])
        d = episode_dir(cfg, v["id"])
        rows.append({"vol_no": v["vol_no"], "label": v["label"], "id": v["id"],
                     "state": job["state"] if job else None,
                     "pages_local": len(page_files_in(d / "pages")) if (d / "pages").exists() else 0,
                     "built": (d / "manga.json").exists()})
    return {"slug": slug, "title": man["title"], "remote_dir": man["remote_dir"],
            "volumes": rows}


def remove(cfg, slug, remote_too=False, log=print):
    """Full delete on the Mac: queue rows, episode dirs (pages, OCR, derived
    data), the ledger footprint of unread volumes (read evidence is kept, as
    the server's DELETE does), the manifest — and with remote_too the PC's
    OCR cache. The scans under the PC's manga root are never touched."""
    import shutil

    from ledger import ledgerctl as lc
    from server import jobqueue as q
    man = load_manifest(cfg, slug)
    conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
    ledger = lc.open_db(cfg["ledger_db"])
    removed = []
    for v in man["volumes"]:
        d = episode_dir(cfg, v["id"])
        if d.exists():
            shutil.rmtree(d)
        lc.purge_episode(ledger, v["id"])
        q.delete_job(conn, v["id"])
        removed.append(v["id"])
    if remote_too:
        mcfg = manga_cfg(cfg)
        base = _win(mcfg["remote_ocr_dir"])
        Remote(mcfg).run(f'if exist "{base}\\{slug}" rmdir /s /q "{base}\\{slug}"')
    shutil.rmtree(manga_dir(cfg, slug), ignore_errors=True)
    log(f"removed manga {slug}: {len(removed)} volume(s)")
    return {"removed": removed, "remote_ocr_removed": remote_too}


# --- CLI ------------------------------------------------------------------------------

def main(argv=None):
    from lib_config import load_config
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("library", help="series + volumes under the PC's manga root")
    p = sub.add_parser("scan", help="volumes under one PC folder")
    p.add_argument("remote_dir")
    p = sub.add_parser("ingest", help="manifest + enqueue volumes (Stage 1 = the worker)")
    p.add_argument("remote_dir")
    p.add_argument("--slug")
    p.add_argument("--title")
    p.add_argument("--volumes", help="e.g. 1,3-5 (default: all)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-drain", action="store_true",
                   help="only enqueue; leave Stage 1 to the server's worker")
    sub.add_parser("list")
    p = sub.add_parser("status")
    p.add_argument("slug")
    p = sub.add_parser("remove")
    p.add_argument("slug")
    p.add_argument("--remote", action="store_true", help="also drop the PC's OCR cache")
    p = sub.add_parser("read-prep", help="numbered-box page renders + manifest for the AI read")
    p.add_argument("episode_id")
    p = sub.add_parser("read-status")
    p.add_argument("episode_id")
    p = sub.add_parser("read-apply", help="rebuild from the agents' reads + re-run coverage")
    p.add_argument("episode_id")
    p = sub.add_parser("rebuild", help="re-emit manga.json from ocr/ + read/ (structure only, no coverage)")
    p.add_argument("episode_id")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    if args.cmd == "library":
        print(json.dumps(library(cfg), ensure_ascii=False, indent=1))
    elif args.cmd == "scan":
        volumes, unparsed = scan(cfg, args.remote_dir, log=log)
        print(json.dumps({"volumes": volumes, "unparsed": unparsed},
                         ensure_ascii=False, indent=1))
    elif args.cmd == "ingest":
        summary = ingest(cfg, args.remote_dir, slug=args.slug, title=args.title,
                         volumes=args.volumes, dry_run=args.dry_run, log=log)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        if not args.dry_run and not args.no_drain and summary["enqueued"]:
            from server import jobqueue as q
            from server.worker import drain
            conn = q.open_queue(Path(cfg["work_dir"]).expanduser() / "queue.db")
            print(json.dumps(drain(cfg, conn, log=log), ensure_ascii=False, indent=1))
    elif args.cmd == "list":
        print(json.dumps([{"slug": m["slug"], "title": m["title"],
                           "volumes": len(m["volumes"])} for m in list_manga(cfg)],
                         ensure_ascii=False, indent=1))
    elif args.cmd == "status":
        print(json.dumps(status(cfg, args.slug), ensure_ascii=False, indent=1))
    elif args.cmd == "remove":
        print(json.dumps(remove(cfg, args.slug, remote_too=args.remote, log=log),
                         ensure_ascii=False, indent=1))
    elif args.cmd == "read-prep":
        man = read_prep(cfg, args.episode_id, log=log)
        todo = [p["stem"] for p in man["pages"] if p["blocks"] and not p["done"]]
        print(json.dumps({"episode_id": args.episode_id, "to_read": todo,
                          "manifest": str(episode_dir(cfg, args.episode_id) / "ocr" / "read" / "manifest.json")},
                         ensure_ascii=False, indent=1))
    elif args.cmd == "read-status":
        print(json.dumps(read_status(cfg, args.episode_id), ensure_ascii=False, indent=1))
    elif args.cmd == "read-apply":
        print(json.dumps(read_apply(cfg, args.episode_id, log=log), ensure_ascii=False, indent=1))
    elif args.cmd == "rebuild":
        print(json.dumps(rebuild(cfg, args.episode_id, log=log), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
