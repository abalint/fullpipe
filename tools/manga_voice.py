#!/usr/bin/env python3
"""manga_voice — a voice track for a manga volume: every speech bubble as a
short ElevenLabs clip, pre-rendered on the Mac and played by the phone's
reader on demand (offline, no round-trip at read time).

Why pre-render: a volume is 10–30k characters (~1,000–2,000 bubbles), which
is one to three dollars at list price — cheaper than the read agents that
wrote the script — while generating at tap time would need the Mac, the
tailnet and ElevenLabs all reachable from the train. So the whole volume
is synthesized once, after the AI read, and ships with the pages.

Who says what, and how, is a script. Since 2026-10-04 the reading agents
write it in the same pass as the transcription (READ_PROMPT.md — they see
the page, so they know who is talking): each read block carries
`speaker` / `say`, and each read file a `cast`. A volume read before that
gets the script from a text-only pass (skills/manga/VOICE_PROMPT.md →
voice/pages/<stem>.json); `apply` takes the read's fields first, the
voice pages second, and the lettered text last. The fields:
  - `speaker`: the cast name the bubble belongs to (a per-series cast,
    kept in <work_dir>/manga/<slug>/cast.json, so Nagatoro sounds the same
    in volume 3 as in volume 1), or null for narration / unknown;
  - `say`: the text to synthesize — the bubble as lettered, except that
    names are written in kana (TTS guesses name readings) and anything
    that trips the voice (stutter dashes, drawn-out ー runs, 「」 quotes,
    sound-effect noise) is cleaned up; null = don't voice (a sign, a title,
    a sound effect, a page number).
Each bubble becomes one clip (a bubble with two sentences reads as one
line), keyed by page stem + mokuro block index, and the phone maps the
tapped sentence idx back to its clip.

Stages under <work_dir>/episodes/manga_<slug>_vNN/voice/:
    manifest.json     prep  — per page: the bubbles (k, sentence idxs, text,
                              gloss) + the series cast so far + the synopsis
    pages/<stem>.json agents — {blocks: {k: {speaker, say}}, cast: [...]}
    script.json       apply — the compiled script: clips [{page, stem, k,
                              sents, speaker, say}] + the merged cast
    clips/<stem>_<k>.mp3  tts — one clip per bubble
    clips.json        tts   — what was generated (signature per clip, so a
                              re-run only re-renders bubbles whose text,
                              voice or model changed)
    index.json        tts   — what the phone fetches (GET /manga/{id}/voice):
                              clips with sentence idxs, speaker, duration
<work_dir>/manga/<slug>/cast.json   the series cast: name → {kana, gender,
                              age, dialect, note, voice_id}; voice_id null =
                              the most specific default voice (config.json →
                              manga.voice.default_voices: "female:old",
                              "male:kansai", "female", "neutral", …)

The API key is ELEVENLABS_API_KEY (.env), the same one the ASR path uses.

CLI (also reachable as `python -m tools.manga voice …`):
    python -m tools.manga_voice prep   manga_nagatoro_v01        # manifest for the script agents
    python -m tools.manga_voice status manga_nagatoro_v01
    python -m tools.manga_voice apply  manga_nagatoro_v01        # pages/*.json → script.json (+ cast merge)
    python -m tools.manga_voice tts    manga_nagatoro_v01 [--dry-run] [--pages 1-20] [--force] [--model eleven_v4]
    python -m tools.manga_voice cast   nagatoro [--set 長瀞=<voice_id>] [--unset 長瀞]
    python -m tools.manga_voice voices [--language ja] [--search Kyoko]  # the account's voices
    python -m tools.manga_voice usage                                    # characters left this period
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._staging import episode_dir, read_json, write_json  # noqa: E402
from tools import manga as MG  # noqa: E402

API_BASE = "https://api.elevenlabs.io"
DEFAULTS = {
    "model": "eleven_v4",            # 2026-09-28 model: natural Japanese, takes previous/next_text (v3 refuses them)
    "output_format": "mp3_44100_64",  # ~8 kB/s; a 2 s bubble ≈ 16 kB, a volume ≈ 10 MB
    "language": "ja",
    "concurrency": 3,                # Starter's pool for the non-Flash models
    "context": True,                 # previous_text / next_text = the neighbouring bubbles
    "seed": None,
    "voice_settings": None,          # e.g. {"stability": 0.5}; None = the voice's own
    "default_voices": {},            # {"female": id, "male": id, "neutral": id, "female:old": id, "male:kansai": id, …}
    "timeout": 120,
    "retries": 6,
}
# credits per character by model family (ElevenLabs pricing page, 2026-10)
HALF_CREDIT = ("flash", "turbo")
VOICE_DIR = "voice"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- config / paths -------------------------------------------------------------

def voice_cfg(cfg):
    out = dict(DEFAULTS)
    out.update((cfg.get("manga") or {}).get("voice") or {})
    return out


def voice_dir(cfg, episode_id, create=False):
    d = episode_dir(cfg, episode_id) / VOICE_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def cast_path(cfg, slug):
    return MG.manga_dir(cfg, slug) / "cast.json"


def load_cast(cfg, slug):
    p = cast_path(cfg, slug)
    if not p.exists():
        return {"slug": slug, "cast": {}}
    d = read_json(p)
    d.setdefault("cast", {})
    return d


def save_cast(cfg, doc):
    write_json(cast_path(cfg, doc["slug"]), doc)


def credits_per_char(model):
    return 0.5 if any(m in (model or "") for m in HALF_CREDIT) else 1.0


# --- the bubbles of a volume ------------------------------------------------------

def bubbles(cfg, episode_id):
    """manga.json blocks + transcript text (+ the read's bubble glosses) →
    [{page, stem, file, k, sents, text, gloss}] in reading order."""
    ep_dir = episode_dir(cfg, episode_id)
    doc = read_json(ep_dir / "manga.json")
    sentences = read_json(ep_dir / "transcript.json")["sentences"]
    glosses = {}
    lines = ep_dir / "read" / "lines.json"
    if lines.exists():
        for ln in read_json(lines).get("lines", []):
            if isinstance(ln.get("idx"), int) and ln.get("gloss"):
                glosses[ln["idx"]] = ln["gloss"]
    out = []
    for pg in doc["pages"]:
        stem = Path(pg["file"]).stem
        for b in pg["blocks"]:
            sents = list(b.get("sents") or [])
            text = "".join(sentences[i]["text"] for i in sents if i < len(sentences))
            if not text:
                continue
            gloss = next((glosses[i] for i in sents if i in glosses), None)
            out.append({"page": pg["n"], "stem": stem, "file": pg["file"], "k": b["k"],
                        "sents": sents, "text": text, "gloss": gloss})
    return doc, out


# --- prep / status / apply -------------------------------------------------------

def read_scripts(cfg, episode_id):
    """The voice fields the reading agents wrote (ocr/read/<stem>.json
    blocks with a `say` key): {stem: {k: {speaker, say}}}. A page whose
    read has no voice fields (read before 2026-10-04) is absent."""
    out = {}
    for stem, blocks in MG.load_read(episode_dir(cfg, episode_id) / "ocr").items():
        got = {k: {"speaker": v.get("speaker"), "say": v.get("say")}
               for k, v in blocks.items() if "say" in v}
        if got:
            out[stem] = got
    return out


def voice_prep(cfg, episode_id, log=print):
    """voice/manifest.json for the script agents: every bubble with its
    text and gloss, page by page, plus the series cast so far and the
    curate pass's synopsis (who the people are). Pages whose read already
    carries the voice fields are listed as done."""
    doc, bbs = bubbles(cfg, episode_id)
    vdir = voice_dir(cfg, episode_id, create=True)
    (vdir / "pages").mkdir(exist_ok=True)
    ep_dir = episode_dir(cfg, episode_id)
    synopsis = None
    curate = ep_dir / "curate.json"
    if curate.exists():
        synopsis = (read_json(curate).get("synopsis") or None)
    castdoc = load_cast(cfg, doc["slug"])
    merge_cast(castdoc["cast"], MG.load_read_cast(ep_dir / "ocr"))
    cast = castdoc["cast"]
    from_read = read_scripts(cfg, episode_id)
    pages = {}
    for b in bbs:
        pg = pages.setdefault(b["stem"], {"n": b["page"], "file": b["file"], "stem": b["stem"],
                                          "done": b["stem"] in from_read
                                          or (vdir / "pages" / f"{b['stem']}.json").exists(),
                                          "blocks": []})
        pg["blocks"].append({"k": b["k"], "sents": b["sents"], "text": b["text"],
                             "gloss": b["gloss"]})
    manifest = {
        "episode_id": episode_id, "slug": doc["slug"], "title": doc["title"],
        "series_title": doc["series_title"], "vol_no": doc["vol_no"],
        "read_by": doc.get("read_by"), "synopsis": synopsis,
        "cast": cast,
        "pages": sorted(pages.values(), key=lambda p: p["n"]),
    }
    write_json(vdir / "manifest.json", manifest)
    todo = [p for p in manifest["pages"] if not p["done"]]
    log(f"voice prep: {len(manifest['pages'])} pages with bubbles, {len(from_read)} scripted by "
        f"the read, {len(todo)} to script, {sum(len(p['blocks']) for p in todo)} bubbles, "
        f"cast so far: {len(cast)} → {vdir}")
    if doc.get("read_by") != "opus":
        log("WARNING: this volume has not had the AI read — the script would voice mokuro's draft")
    return manifest


def script_status(cfg, episode_id):
    vdir = voice_dir(cfg, episode_id)
    man = vdir / "manifest.json"
    pages = read_json(man)["pages"] if man.exists() else []
    from_read = read_scripts(cfg, episode_id)
    scripted = [p for p in pages
                if p["stem"] in from_read or (vdir / "pages" / f"{p['stem']}.json").exists()]
    clips = read_json(vdir / "clips.json") if (vdir / "clips.json").exists() else {}
    script = read_json(vdir / "script.json") if (vdir / "script.json").exists() else None
    return {"episode_id": episode_id, "pages": len(pages), "scripted": len(scripted),
            "applied": script is not None,
            "clips_scripted": len(script["clips"]) if script else 0,
            "clips_rendered": len(clips),
            "indexed": (vdir / "index.json").exists()}


_ELLIPSIS = re.compile(r"(?:\.{2,}|．{2,}|…{2,}|・{3,})")
_QUOTES = str.maketrans("", "", "「」『』（）()　 \t\r\n")


def tts_text(say):
    """The agent's `say` → what goes on the wire: no quotes/brackets, no
    whitespace, one … per ellipsis run. Content is the agent's call."""
    s = str(say or "")
    s = _ELLIPSIS.sub("…", s)
    s = s.translate(_QUOTES)
    return s.strip()


def load_script_pages(vdir):
    out = {}
    for p in sorted((vdir / "pages").glob("*.json")):
        try:
            d = read_json(p)
        except ValueError:
            continue
        out[p.stem] = d if isinstance(d, dict) else {}
    return out


def merge_cast(cast, entries):
    """Agents' cast entries ([{name, kana, gender, age, note}]) into the
    series cast (name → entry); a known name keeps its voice_id and
    fills only the fields it lacked. Returns the names added."""
    added = []
    for e in entries or []:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        name = str(e["name"]).strip()
        cur = cast.get(name)
        if cur is None:
            cast[name] = {"kana": e.get("kana"), "gender": e.get("gender"),
                          "age": e.get("age"), "dialect": e.get("dialect") or None,
                          "note": e.get("note"), "voice_id": None}
            added.append(name)
        else:
            for f in ("kana", "gender", "age", "dialect", "note"):
                if not cur.get(f) and e.get(f):
                    cur[f] = e[f]
            cur.setdefault("voice_id", None)
    return added


def voice_apply(cfg, episode_id, log=print, min_fraction=0.95):
    """voice/pages/*.json → voice/script.json; the agents' cast entries are
    merged into the series cast. Refuses while fewer than min_fraction of
    the pages are scripted (the rest would fall back to raw text)."""
    vdir = voice_dir(cfg, episode_id)
    man_p = vdir / "manifest.json"
    if not man_p.exists():
        raise RuntimeError("run prep first")
    man = read_json(man_p)
    pages = load_script_pages(vdir)
    from_read = read_scripts(cfg, episode_id)
    have = [p for p in man["pages"] if p["stem"] in pages or p["stem"] in from_read]
    if man["pages"] and len(have) < len(man["pages"]) * min_fraction:
        raise RuntimeError(f"only {len(have)}/{len(man['pages'])} pages scripted — "
                           "run the agents over the rest first")
    castdoc = load_cast(cfg, man["slug"])
    added = merge_cast(castdoc["cast"], MG.load_read_cast(episode_dir(cfg, episode_id) / "ocr"))
    for stem, d in pages.items():
        added += merge_cast(castdoc["cast"], d.get("cast"))
    clips, skipped, defaulted, unknown = [], 0, 0, {}
    for p in man["pages"]:
        got = {**((pages.get(p["stem"]) or {}).get("blocks") or {}),
               **from_read.get(p["stem"], {})}  # the read's fields win
        for b in p["blocks"]:
            entry = got.get(str(b["k"]))
            if entry is None:
                say = tts_text(b["text"])  # the agent skipped it: say the lettered text
                speaker = None
                defaulted += 1
            else:
                if not isinstance(entry, dict) or not entry.get("say"):
                    skipped += 1
                    continue
                say = tts_text(entry["say"])
                speaker = (entry.get("speaker") or None)
                if speaker:
                    speaker = str(speaker).strip()
                    if speaker not in castdoc["cast"]:
                        unknown[speaker] = unknown.get(speaker, 0) + 1
            if not say:
                skipped += 1
                continue
            clips.append({"page": p["n"], "stem": p["stem"], "k": b["k"], "sents": b["sents"],
                          "speaker": speaker, "say": say, "text": b["text"]})
    for name, n in unknown.items():  # a speaker the agents named but never cast
        castdoc["cast"][name] = {"kana": None, "gender": None, "age": None,
                                 "note": f"(uncast: {n} bubbles)", "voice_id": None}
        added.append(name)
    castdoc["updated_at"] = now_iso()
    save_cast(cfg, castdoc)
    script = {"episode_id": episode_id, "slug": man["slug"], "built_at": now_iso(),
              "pages_scripted": len(have), "clips": clips,
              "skipped": skipped, "defaulted": defaulted}
    write_json(vdir / "script.json", script)
    chars = sum(len(c["say"]) for c in clips)
    log(f"script: {len(clips)} clips / {chars} chars from {len(have)} pages "
        f"({skipped} bubbles not voiced, {defaulted} unscripted → lettered text); "
        f"cast {len(castdoc['cast'])} (+{len(added)}: {', '.join(added) or '—'}) → {cast_path(cfg, man['slug'])}")
    return {"clips": len(clips), "chars": chars, "skipped": skipped, "defaulted": defaulted,
            "cast_added": added, "cast": castdoc["cast"]}


# --- voices ------------------------------------------------------------------------

def default_voice_keys(entry):
    """The default_voices keys tried for an uncast speaker, most specific
    first: gender:age:dialect, gender:dialect, gender:age, dialect, gender,
    then neutral / narration / default. So `"female:old"` catches every
    ばあさん, `"male:kansai"` every Kansai man, with no per-character work."""
    g, a, d = ((entry or {}).get(k) or None for k in ("gender", "age", "dialect"))
    keys = []
    if g and a and d:
        keys.append(f"{g}:{a}:{d}")
    if g and d:
        keys.append(f"{g}:{d}")
    if g and a:
        keys.append(f"{g}:{a}")
    if d:
        keys.append(d)
    if g:
        keys.append(g)
    return keys + ["neutral", "narration", "default"]


def resolve_voice(cast, speaker, vcfg):
    """(voice_id, label) for a bubble: the cast member's own voice, else the
    most specific default for their gender / age / dialect
    (default_voice_keys), else the neutral/narration default. None when
    nothing is configured."""
    dv = vcfg.get("default_voices") or {}
    entry = cast.get(speaker) if speaker else None
    if entry and entry.get("voice_id"):
        return entry["voice_id"], speaker
    for key in default_voice_keys(entry):
        if dv.get(key):
            return dv[key], f"default:{key}"
    return None, None


# --- the API -------------------------------------------------------------------------

class ElevenLabs:
    """The slice of the ElevenLabs REST API this needs (requests; no SDK)."""

    def __init__(self, api_key=None, base=API_BASE, timeout=120, retries=6, sleep=time.sleep):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set (.env)")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self._no_context = False  # set once the model rejects previous/next_text
        self._lock = threading.Lock()

    def _headers(self):
        return {"xi-api-key": self.api_key, "accept": "application/json"}

    def _request(self, method, path, **kw):
        import requests
        last = None
        for attempt in range(self.retries):
            r = requests.request(method, self.base + path, headers=self._headers(),
                                 timeout=self.timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                last = r
                wait = float(r.headers.get("retry-after") or min(2 ** attempt, 30))
                self.sleep(wait)
                continue
            if r.status_code == 401:
                raise RuntimeError(f"ElevenLabs 401: {r.text[:200]}")  # bad key, or a key scoped without this permission
            return r
        raise RuntimeError(f"ElevenLabs: gave up after {self.retries} tries "
                           f"({last.status_code if last is not None else '?'}: "
                           f"{(last.text if last is not None else '')[:200]})")

    def subscription(self):
        r = self._request("GET", "/v1/user/subscription")
        r.raise_for_status()
        return r.json()

    def voices(self, language=None, search=None, page_size=100):
        params = {"page_size": page_size}
        if language:
            params["language"] = language
        if search:
            params["search"] = search
        out, token = [], None
        while True:
            if token:
                params["next_page_token"] = token
            r = self._request("GET", "/v2/voices", params=params)
            r.raise_for_status()
            d = r.json()
            out += d.get("voices") or []
            token = d.get("next_page_token") if d.get("has_more") else None
            if not token:
                return out

    def tts(self, voice_id, text, model_id, language=None, previous_text=None, next_text=None,
            output_format="mp3_44100_64", seed=None, voice_settings=None):
        body = {"text": text, "model_id": model_id}
        if language:
            body["language_code"] = language
        if seed is not None:
            body["seed"] = int(seed)
        if voice_settings:
            body["voice_settings"] = voice_settings
        ctx = {}
        if previous_text:
            ctx["previous_text"] = previous_text
        if next_text:
            ctx["next_text"] = next_text
        for use_ctx in (True, False):
            if use_ctx and (self._no_context or not ctx):
                continue
            payload = {**body, **(ctx if use_ctx else {})}
            r = self._request("POST", f"/v1/text-to-speech/{voice_id}",
                              params={"output_format": output_format}, json=payload)
            if (r.status_code in (400, 422) and use_ctx
                    and ("previous_text" in r.text or "next_text" in r.text)):
                with self._lock:
                    self._no_context = True  # no request stitching on this model (eleven_v3: 400 unsupported_model)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"ElevenLabs tts {r.status_code}: {r.text[:300]}")
            return r.content, r.headers.get("request-id")
        raise RuntimeError("ElevenLabs tts: unreachable")


# --- render ---------------------------------------------------------------------------

def clip_name(stem, k):
    return f"{stem}_{k}.mp3"


def clip_sig(model, voice_id, say, output_format):
    return hashlib.sha1(f"{model}|{voice_id}|{say}|{output_format}".encode("utf-8")).hexdigest()[:12]


def clip_ms(path):
    """Duration via ffprobe when it's around (the Mac has ffmpeg for the
    transcodes); None otherwise — the phone can read it off the element."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return int(float(out) * 1000)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def plan(cfg, episode_id, model=None, force=False, pages=None):
    """What `tts` would render: every script clip with its voice and
    signature, split into todo / kept. Raises when a speaker has no voice."""
    vcfg = voice_cfg(cfg)
    model = model or vcfg["model"]
    vdir = voice_dir(cfg, episode_id)
    script_p = vdir / "script.json"
    if not script_p.exists():
        raise RuntimeError("run apply first")
    script = read_json(script_p)
    cast = load_cast(cfg, script["slug"])["cast"]
    done = read_json(vdir / "clips.json") if (vdir / "clips.json").exists() else {}
    lo, hi = pages if pages else (None, None)
    todo, kept, missing = [], [], {}
    clips = script["clips"]
    for i, c in enumerate(clips):
        if lo is not None and not (lo <= c["page"] + 1 <= hi):
            continue
        voice_id, label = resolve_voice(cast, c.get("speaker"), vcfg)
        if not voice_id:
            key = c.get("speaker") or "(no speaker)"
            missing[key] = missing.get(key, 0) + 1
            continue
        file = clip_name(c["stem"], c["k"])
        sig = clip_sig(model, voice_id, c["say"], vcfg["output_format"])
        item = {**c, "file": file, "voice_id": voice_id, "voice": label, "sig": sig,
                "chars": len(c["say"])}
        if vcfg.get("context"):
            prev = clips[i - 1] if i > 0 and clips[i - 1]["page"] == c["page"] else None
            nxt = clips[i + 1] if i + 1 < len(clips) and clips[i + 1]["page"] == c["page"] else None
            item["previous_text"] = prev["say"] if prev else None
            item["next_text"] = nxt["say"] if nxt else None
        have = done.get(file)
        if not force and have and have.get("sig") == sig and (vdir / "clips" / file).exists():
            kept.append(item)
        else:
            todo.append(item)
    if missing:
        raise RuntimeError("no voice for: " + ", ".join(f"{k} ({n})" for k, n in missing.items())
                           + f" — set them in {cast_path(cfg, script['slug'])} or "
                           "manga.voice.default_voices in config.json")
    return {"model": model, "todo": todo, "kept": kept, "script": script, "vcfg": vcfg,
            "chars": sum(c["chars"] for c in todo),
            "credits": sum(c["chars"] for c in todo) * credits_per_char(model)}


def synthesize(cfg, episode_id, client=None, dry_run=False, model=None, force=False,
               pages=None, log=print):
    """Render the script's clips (only the ones missing or changed), record
    them in clips.json and rebuild index.json. Checks the account's
    remaining characters first and refuses a run that would not fit."""
    pl = plan(cfg, episode_id, model=model, force=force, pages=pages)
    vcfg, model = pl["vcfg"], pl["model"]
    by_voice = {}
    for c in pl["todo"]:
        by_voice[c["voice"]] = by_voice.get(c["voice"], 0) + c["chars"]
    summary = {"episode_id": episode_id, "model": model, "to_render": len(pl["todo"]),
               "kept": len(pl["kept"]), "chars": pl["chars"], "credits": pl["credits"],
               "by_voice": by_voice}
    if dry_run:
        log(f"dry run: {len(pl['todo'])} clips / {pl['chars']} chars "
            f"(≈{pl['credits']:.0f} credits on {model}), {len(pl['kept'])} already rendered")
        return summary
    vdir = voice_dir(cfg, episode_id)
    if pl["todo"]:
        client = client or ElevenLabs(timeout=vcfg["timeout"], retries=vcfg["retries"])
        sub = None
        try:
            sub = client.subscription()
        except Exception as e:  # noqa: BLE001 — usage is advisory (a key without user_read)
            log(f"(could not read the subscription: {str(e)[:120]})")
        if sub:
            left = int(sub.get("character_limit", 0)) - int(sub.get("character_count", 0))
            summary["characters_left_before"] = left
            log(f"account: {sub.get('tier')} — {left} characters left this period")
            if pl["credits"] > left and not force:
                raise RuntimeError(f"needs ≈{pl['credits']:.0f} credits, {left} left — "
                                   "top up, narrow --pages, or --force")
    (vdir / "clips").mkdir(parents=True, exist_ok=True)
    done_p = vdir / "clips.json"
    done = read_json(done_p) if done_p.exists() else {}
    lock = threading.Lock()
    rendered, errors = 0, []

    def one(c):
        audio, rid = client.tts(
            c["voice_id"], c["say"], model, language=vcfg.get("language"),
            previous_text=c.get("previous_text"), next_text=c.get("next_text"),
            output_format=vcfg["output_format"], seed=vcfg.get("seed"),
            voice_settings=vcfg.get("voice_settings"))
        path = vdir / "clips" / c["file"]
        path.write_bytes(audio)
        return c, {"sig": c["sig"], "chars": c["chars"], "bytes": len(audio),
                   "ms": clip_ms(path), "voice_id": c["voice_id"], "voice": c["voice"],
                   "speaker": c.get("speaker"), "model": model, "request_id": rid,
                   "at": now_iso()}

    todo = pl["todo"]
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, int(vcfg["concurrency"]))) as ex:
            futs = {ex.submit(one, c): c for c in todo}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    _, rec = f.result()
                except Exception as e:  # noqa: BLE001 — keep the rest going
                    errors.append({"file": c["file"], "error": str(e)[:300]})
                    log(f"  ✗ {c['file']}: {str(e)[:120]}")
                    continue
                with lock:
                    done[c["file"]] = rec
                    rendered += 1
                    if rendered % 25 == 0 or rendered == len(todo):
                        write_json(done_p, done)
                        log(f"  {rendered}/{len(todo)} clips")
        write_json(done_p, done)
    # drop clips the script no longer has (re-scripted bubble, re-read page)
    wanted = {c["file"] for c in pl["todo"] + pl["kept"]}
    if pages is None:
        for file in [f for f in done if f not in wanted]:
            done.pop(file)
            try:
                (vdir / "clips" / file).unlink()
            except FileNotFoundError:
                pass
        write_json(done_p, done)
    idx = write_index(cfg, episode_id, model=model)
    summary.update({"rendered": rendered, "errors": errors, "indexed": len(idx["clips"]),
                    "chars_sent": sum(done[c["file"]]["chars"] for c in todo if c["file"] in done)})
    log(f"rendered {rendered}/{len(todo)} clips ({len(errors)} errors); "
        f"index: {len(idx['clips'])} clips")
    return summary


def write_index(cfg, episode_id, model=None):
    """voice/index.json — the phone's view: one entry per rendered clip
    with its sentence idxs, so the reader maps a tapped sentence to a file."""
    vdir = voice_dir(cfg, episode_id)
    script = read_json(vdir / "script.json")
    done = read_json(vdir / "clips.json") if (vdir / "clips.json").exists() else {}
    clips = []
    for c in script["clips"]:
        file = clip_name(c["stem"], c["k"])
        rec = done.get(file)
        if not rec or not (vdir / "clips" / file).exists():
            continue
        clips.append({"file": file, "page": c["page"], "k": c["k"], "sents": c["sents"],
                      "speaker": c.get("speaker"), "voice": rec.get("voice"),
                      "ms": rec.get("ms"), "bytes": rec.get("bytes")})
    idx = {"episode_id": episode_id, "built_at": now_iso(),
           "model": model or voice_cfg(cfg)["model"],
           "clips": clips, "bytes": sum(c.get("bytes") or 0 for c in clips)}
    write_json(vdir / "index.json", idx)
    return idx


# --- CLI ------------------------------------------------------------------------------

def parse_pages(spec):
    if not spec:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if not m:
        raise ValueError(f"bad --pages {spec!r} (want N or N-M, 1-based)")
    lo = int(m.group(1))
    hi = int(m.group(2) or lo)
    return lo, hi


def main(argv=None):
    from lib_config import load_config
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, h in (("prep", "manifest for the script agents"), ("status", ""),
                    ("apply", "pages/*.json → script.json, cast merge"),
                    ("index", "re-emit index.json from the clips on disk")):
        p = sub.add_parser(name, help=h)
        p.add_argument("episode_id")
    p = sub.add_parser("tts", help="render the script's clips with ElevenLabs")
    p.add_argument("episode_id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="re-render everything; ignore the usage check")
    p.add_argument("--model")
    p.add_argument("--pages", help="1-based page range, e.g. 1-20")
    p = sub.add_parser("cast", help="show / set the series cast's voices")
    p.add_argument("slug")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VOICE_ID")
    p.add_argument("--unset", action="append", default=[], metavar="NAME")
    p = sub.add_parser("voices", help="the account's voices")
    p.add_argument("--language")
    p.add_argument("--search")
    sub.add_parser("usage", help="characters used / left this period")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    out = None
    if args.cmd == "prep":
        man = voice_prep(cfg, args.episode_id, log=log)
        out = {"episode_id": args.episode_id,
               "to_script": [p["stem"] for p in man["pages"] if not p["done"]],
               "cast": sorted(man["cast"]),
               "manifest": str(voice_dir(cfg, args.episode_id) / "manifest.json")}
    elif args.cmd == "status":
        out = script_status(cfg, args.episode_id)
    elif args.cmd == "apply":
        out = voice_apply(cfg, args.episode_id, log=log)
    elif args.cmd == "index":
        out = {"indexed": len(write_index(cfg, args.episode_id)["clips"])}
    elif args.cmd == "tts":
        out = synthesize(cfg, args.episode_id, dry_run=args.dry_run, model=args.model,
                         force=args.force, pages=parse_pages(args.pages), log=log)
    elif args.cmd == "cast":
        doc = load_cast(cfg, args.slug)
        for s in args.set:
            name, _, vid = s.partition("=")
            doc["cast"].setdefault(name, {"kana": None, "gender": None, "age": None,
                                          "note": None})["voice_id"] = vid or None
        for name in args.unset:
            if name in doc["cast"]:
                doc["cast"][name]["voice_id"] = None
        if args.set or args.unset:
            doc["updated_at"] = now_iso()
            save_cast(cfg, doc)
        out = doc
    elif args.cmd == "voices":
        vs = ElevenLabs().voices(language=args.language, search=args.search)
        out = [{"voice_id": v.get("voice_id"), "name": v.get("name"),
                "labels": v.get("labels"), "category": v.get("category"),
                "languages": [x.get("language") for x in (v.get("verified_languages") or [])],
                "preview": v.get("preview_url")} for v in vs]
    elif args.cmd == "usage":
        s = ElevenLabs().subscription()
        out = {k: s.get(k) for k in ("tier", "status", "character_count", "character_limit",
                                     "next_character_count_reset_unix", "voice_limit",
                                     "voice_slots_used")}
        out["characters_left"] = int(s.get("character_limit", 0)) - int(s.get("character_count", 0))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
