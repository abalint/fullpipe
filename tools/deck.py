#!/usr/bin/env python3
"""deck — curated card picks → Anki cards with native audio + a video frame.

Cuts the native audio clip to the MERGED sentence span (±0.5s pad) — the
resolution of the subs2srs fragment flaw (DESIGN.md — Card philosophy) — and
grabs a still frame from the sentence midpoint (from the phone-staged
video.mp4, or a local video source; skipped when the episode is audio-only),
and pushes cards live via AnkiConnect (primary: createModel/createDeck/
addNote → note ids the ledger can lapse-poll). `--apkg` is the offline
fallback (genanki, stable guids). Every minted card is registered in the
ledger (mined_card evidence + cards row), then `promote` runs.

Every push path runs three quality guards (this is the layer that sees them
all — server close-out, direct-mode CLI, .apkg fallback). There is no numeric
cap anywhere: card volume is an outcome of the curation bar, never a count.
  * standing high-interest lemmas (ledger tap_interest) jump the queue;
  * clip spans outside 1.5–15s are rejected (a bad audio card regardless of
    text);
  * each cut clip is re-transcribed on the GPU service (asr.gpu_url) and the
    card is dropped unless the sentence is audible in the clip
    (deck.audio_gate: enabled / min_match; fails open when the desktop is
    unreachable).

Input picks.json — the /immerse curate output:
    [{"lemma": "縄張り", "sentence_idx": 9, "reading": "なわばり",
      "english": "optional gloss/translation",
      "sentence_furigana": "optional full sentence in Anki furigana format,
          readings written by the curating LLM (never a dictionary):
          " 縄張[なわば]りを 守[まも]る。" — used as the sentence field when
          it strips back to the transcript sentence, else dropped with a log",
      "notes": "optional English usage/nuance notes on the target word",
      "context": "optional English note on what the video was discussing"}, ...]

By default cards use the built-in "fullPipe Sentence Mining" model (created
on demand). To mint onto the user's own note type instead, set in config:

    "deck": {"name": "MinePrime",
             "note_type": "Sentence Cards",
             "field_map": {"sentence": "Sentence", "audio": "Audio",
                           "english": "English"}}

field_map keys: sentence · audio · english · image · notes · context ·
lemma · reading · source · sequence — map only the fields the note type has; a custom
note_type must already exist in Anki (only the built-in model is
auto-created). The --apkg fallback always uses the built-in model (an
offline .apkg can't reuse a collection's note type).

CLI:
    python -m tools.deck EPISODE_ID picks.json [--apkg] [--config PATH]
"""

import argparse
import difflib
import re
import sys
import unicodedata
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.audio import probe_audio_duration, slice_audio, MIN_SLICE_DURATION  # noqa: E402
from engine.frames import extract_frame  # noqa: E402
from engine.local_file import get_video_path  # noqa: E402
from engine.transcriber import (  # noqa: E402
    GpuTranscriber, GpuUnavailableError, TranscriptionError)
from lib_config import load_config  # noqa: E402
from ledger import ledgerctl as lc  # noqa: E402
from ledger.anki_known import anki_request  # noqa: E402
from tools._staging import episode_dir, load_transcript, read_json  # noqa: E402
from tools.select import MIN_CLIP, MAX_CLIP  # noqa: E402

CLIP_PAD = 0.5
# Every card clip is loudness-normalized to this integrated loudness so review
# volume is consistent across episodes/sources. Override with config
# deck.clip_target_lufs; set it to null to disable normalization.
CLIP_TARGET_LUFS = -16.0

MODEL_NAME = "fullPipe Sentence Mining"
# New fields are only ever appended so the field indices of the original six
# stay put — the .apkg guid keys off Sequence (index 5), which must not shift.
MODEL_FIELDS = ["Expression", "Audio", "Lemma", "Reading", "Source",
                "Sequence", "Image", "Notes", "Context"]
MODEL_CSS = """.card {
  font-family: "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-size: 26px; text-align: center; }
.card img { max-width: 100%; max-height: 320px; border-radius: 6px;
  margin-bottom: 0.5em; }
.lemma { color: #4a90d9; font-size: 30px; }
.notes, .context { font-size: 16px; text-align: left; margin-top: 0.8em; }
.context { color: #666; font-style: italic; }
.meta { font-size: 14px; color: #888; margin-top: 1em; }
/* Furigana stays hidden until the reveal toggle — reading recall first. */
.expression ruby rt { opacity: 0; }
.expression.show-furi ruby rt { opacity: 1; }
.furi-toggle { font-size: 14px; color: #4a90d9; text-decoration: none; }"""
MODEL_TEMPLATES = [{
    "Name": "Card 1",
    "Front": ("{{#Image}}{{Image}}<br>{{/Image}}"
              "<span class=expression>{{kanji:Expression}}</span><br>{{Audio}}"),
    "Back": ("{{#Image}}{{Image}}<br>{{/Image}}"
             "<span class=expression id=expr>{{furigana:Expression}}</span> "
             "<a class=furi-toggle href=# onclick=\""
             "document.getElementById('expr').classList.toggle('show-furi');"
             "return false;\">ふりがな</a><br>{{Audio}}"
             "<hr id=answer>"
             "<div class=lemma>{{Lemma}}【{{Reading}}】</div>"
             "{{#Notes}}<div class=notes>{{Notes}}</div>{{/Notes}}"
             "{{#Context}}<div class=context>{{Context}}</div>{{/Context}}"
             "<div class=meta>{{Source}}</div>"),
}]

_GENANKI_MODEL_ID = 1998244353  # distinct from engine.anki's subs2srs model

# Maps pick-payload keys onto the built-in model's fields; a config
# deck.field_map replaces this when minting onto the user's own note type.
DEFAULT_FIELD_MAP = {
    "sentence": "Expression", "audio": "Audio", "lemma": "Lemma",
    "reading": "Reading", "source": "Source", "sequence": "Sequence",
    "image": "Image", "notes": "Notes", "context": "Context",
}

# Curation fields that must be filled for a card to be worth minting — a
# blank-backed card is a worse outcome than no card (the user's quality bar:
# card volume is never the goal). Only enforced for fields the target note
# type actually has, so a minimal field_map doesn't block every card.
GLOSS_FIELDS = ("english", "notes", "context")


def _required_gloss(field_map):
    return tuple(f for f in GLOSS_FIELDS if f in field_map)


_FURIGANA_BRACKETS = re.compile(r"\[[^\]]*\]")


def furigana_matches(annotated, raw):
    """True when the annotated sentence strips back to the raw one.

    Anki furigana format: readings in brackets after each kanji run, an ASCII
    space delimiting the run ("犬[いぬ]が 縄張[なわば]り"). Stripping brackets
    and spaces (both are rendering artifacts) must reproduce the transcript
    sentence exactly — the guard against the curating LLM drifting from the
    audio's actual line.
    """
    strip = lambda s: (_FURIGANA_BRACKETS.sub("", s)  # noqa: E731
                       .replace(" ", "").replace("　", ""))
    return strip(annotated) == strip(raw)


def _norm_ja(text):
    """Collapse a Japanese line to its spoken payload for ASR comparison:
    NFKC, drop punctuation/whitespace (rendering, not speech), fold katakana
    to hiragana (ASR vs subtitle script choice isn't a mismatch)."""
    out = []
    for c in unicodedata.normalize("NFKC", text or ""):
        if not c.isalnum():
            continue
        if "ァ" <= c <= "ヶ":
            c = chr(ord(c) - 0x60)
        out.append(c)
    return "".join(out)


def clip_match_ratio(expected, heard):
    """How much of the expected sentence is audible in the clip: matched
    chars / len(expected), both sides normalized. Recall, not symmetric
    similarity — the ±0.5s pad legitimately catches slivers of neighboring
    speech, which must not count against the card."""
    a, b = _norm_ja(expected), _norm_ja(heard)
    if not a or not b:
        return 0.0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(bl.size for bl in m.get_matching_blocks()) / len(a)


class _GpuAudioGate:
    """Push-time clip validation: re-transcribe the cut clip on the desktop
    GPU service and require the card's sentence to actually be audible in it.
    Catches what used to reach Anki and get deleted on first view — mistimed
    spans, BGM-drowned dialogue, silence. Fails open when the desktop is off
    (a dark PC must not block the phone's mark-watched close-out); a real
    transcription failure on a clip fails just that card."""

    def __init__(self, base_url, token, min_match, log):
        self._transcriber = GpuTranscriber(base_url, token=token)
        self.min_match = min_match
        self.log = log
        self.enabled = True

    def __call__(self, clip_path, expected):
        """Returns (ok, detail)."""
        if not self.enabled:
            return True, "gate offline"
        try:
            words = self._transcriber.transcribe(Path(clip_path), "ja")
        except GpuUnavailableError as e:
            self.enabled = False
            self.log(f"  audio gate: GPU service unreachable — "
                     f"pushing remaining cards unvalidated ({e})")
            return True, "gate offline"
        except TranscriptionError as e:
            return False, f"no clean speech in clip ({e})"
        heard = "".join(w.get("text", "") for w in words)
        ratio = clip_match_ratio(expected, heard)
        if ratio < self.min_match:
            return False, (f"clip audio doesn't match text "
                           f"(heard {heard!r}, match {ratio:.2f} < {self.min_match})")
        return True, f"match {ratio:.2f}"


def _resolve_audio_gate(cfg, log):
    """Build the clip gate from config, or None when it can't/shouldn't run.
    On by default whenever asr.gpu_url is configured; deck.audio_gate
    {"enabled": false} switches it off, "min_match" tunes the bar."""
    gate_cfg = cfg.get("deck", {}).get("audio_gate", {})
    url = cfg.get("asr", {}).get("gpu_url")
    if not url or not gate_cfg.get("enabled", True):
        return None
    return _GpuAudioGate(url, cfg.get("asr", {}).get("gpu_token"),
                         float(gate_cfg.get("min_match", 0.6)), log)


def _note_fields(field_map, p, title):
    values = {
        "sentence": p.get("sentence_furigana") or p["sentence"],
        "audio": f"[sound:{p['clip_name']}]",
        "english": p.get("english", ""),
        "image": p.get("image", ""),
        "notes": p.get("notes", ""),
        "context": p.get("context", ""),
        "lemma": p["lemma"],
        "reading": p.get("reading", ""),
        "source": title,
        "sequence": str(p["sentence_idx"]),
    }
    fields = {}
    for key, target in field_map.items():
        if key not in values:
            continue
        # A key may map to several fields (e.g. sentence → Sentence + Japanese
        # when the note type's mandatory first field isn't the primary one).
        for field in ([target] if isinstance(target, str) else target):
            fields[field] = values[key]
    return fields


def _resolve_video(cfg, episode_id, transcript):
    """Return a video path to grab card frames from, or None.

    Prefers the phone-staged ``<episode_dir>/video.mp4`` — the worker lands this
    for every youtube/local-video episode (server.worker.stage_video) — and
    falls back to a local video source. Audio-only episodes have no video, so
    the caller simply mints cards without an image.
    """
    staged = episode_dir(cfg, episode_id) / "video.mp4"
    if staged.exists():
        return staged
    ep = transcript.get("episode", {})
    if ep.get("kind") == "local":
        vp = get_video_path(ep.get("source", ""))
        if vp and Path(vp).exists():
            return Path(vp)
    return None


def _clip_sentence(audio_path, sentence, clip_path, total_duration,
                   target_lufs=CLIP_TARGET_LUFS):
    start = max(0.0, sentence["start"] - CLIP_PAD)
    end = min(total_duration, sentence["end"] + CLIP_PAD)
    if end - start < MIN_SLICE_DURATION:
        raise ValueError(f"degenerate clip for sentence {sentence['idx']}")
    if not Path(clip_path).exists():
        slice_audio(str(audio_path), start, end, str(clip_path),
                    target_lufs=target_lufs)
    return clip_path


def missing_gloss(pick, required):
    """Which required curation fields this pick is missing (empty/absent).

    The curate pass authors english/notes/context per pool entry (immerse
    SKILL.md — the pool schema). That was an instruction with nothing checking
    it, so a curate run that skipped them minted blank-backed cards that
    reached the deck silently. Rescued interest picks (tools.select) never
    carry a gloss at all. Both are caught here."""
    return [f for f in required if not (pick.get(f) or "").strip()]


def _prepare_clips(cfg, episode_id, transcript, picks, log=print,
                   on_progress=None, want_image=True, gate=None,
                   require=()):
    """Cut one native-audio clip per pick. Returns enriched pick dicts.
    on_progress(msg) narrates the per-card work (an ffmpeg encode each) for
    live consumers like the server's queue row. want_image=False skips the
    frame grab entirely (the target note type has no image field).
    gate(clip_path, sentence) → (ok, detail) is the audio validation hook
    (see _GpuAudioGate); a rejected clip just drops its card — fewer, better
    cards is the intended outcome, not a shortfall.
    require: curation fields a pick must carry to become a card (see
    missing_gloss); checked before any ffmpeg/GPU work is spent on it."""
    audio = transcript["episode"]["audio"]
    total = probe_audio_duration(audio)
    if total is None:
        raise RuntimeError(f"cannot probe audio: {audio}")
    by_idx = {s["idx"]: s for s in transcript["sentences"]}
    ep_dir = episode_dir(cfg, episode_id, create=True)
    clips_dir = ep_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    target_lufs = cfg.get("deck", {}).get("clip_target_lufs", CLIP_TARGET_LUFS)

    # Frame source (staged video.mp4 / local video); None → cards get no image.
    video = _resolve_video(cfg, episode_id, transcript) if want_image else None
    images_dir = ep_dir / "images"
    if video is not None:
        images_dir.mkdir(exist_ok=True)

    prepared, ungossed = [], []
    for i, p in enumerate(picks, 1):
        if on_progress:
            on_progress(f"cutting clip {i}/{len(picks)}")
        # Cheapest guard first — no point cutting and GPU-validating a clip
        # for a card that can't carry its own back side.
        gaps = missing_gloss(p, require)
        if gaps:
            log(f"  skip {p['lemma']}: no {'/'.join(gaps)} from the curate pass"
                + (" (rescued interest pick — never glossed)"
                   if p.get("rescued") else ""))
            ungossed.append(p["lemma"])
            continue
        sent = by_idx.get(p["sentence_idx"])
        if sent is None:
            log(f"  skip {p['lemma']}: sentence_idx {p['sentence_idx']} not in transcript")
            continue
        span = sent["end"] - sent["start"]
        if not (MIN_CLIP <= span <= MAX_CLIP):
            # The curation bar (SKILL.md — Selection bar) enforced only in
            # prose until now; a fragment or a rambling span makes a bad card.
            log(f"  skip {p['lemma']}: clip {span:.1f}s outside "
                f"{MIN_CLIP}–{MAX_CLIP}s")
            continue
        clip_name = f"fullPipe_{episode_id}_{p['sentence_idx']:04d}.mp3"
        _clip_sentence(audio, sent, clips_dir / clip_name, total,
                       target_lufs=target_lufs)
        if gate is not None:
            if on_progress:
                on_progress(f"validating clip {i}/{len(picks)}")
            ok, detail = gate(clips_dir / clip_name, sent["text"])
            if not ok:
                log(f"  skip {p['lemma']}: {detail}")
                continue

        # Grab a still from the sentence midpoint. Best-effort: a frame that
        # won't extract must not sink the card (DESIGN.md — Card philosophy).
        image_name = image_path = None
        if video is not None:
            candidate = f"fullPipe_{episode_id}_{p['sentence_idx']:04d}.jpg"
            candidate_path = images_dir / candidate
            midpoint = (sent["start"] + sent["end"]) / 2.0
            try:
                if not candidate_path.exists():
                    extract_frame(video, midpoint, candidate_path)
                image_name = candidate
                image_path = str(candidate_path)
            except RuntimeError as e:
                log(f"  no frame for {p['lemma']}: {e}")

        furigana = p.get("sentence_furigana")
        if furigana and not furigana_matches(furigana, sent["text"]):
            log(f"  furigana mismatch for {p['lemma']}: dropped "
                f"(strips to {_FURIGANA_BRACKETS.sub('', furigana)!r}, "
                f"transcript has {sent['text']!r})")
            furigana = None

        prepared.append({
            **p,
            "sentence": sent["text"],
            "sentence_furigana": furigana,
            "reading": p.get("reading", ""),
            "clip_name": clip_name,
            "clip_path": str(clips_dir / clip_name),
            "image_name": image_name,
            "image_path": image_path,
            "image": f'<img src="{image_name}">' if image_name else "",
        })
    return prepared, ungossed


def _ensure_model(anki_call):
    if MODEL_NAME not in anki_call("modelNames"):
        anki_call("createModel",
                  modelName=MODEL_NAME,
                  inOrderFields=MODEL_FIELDS,
                  css=MODEL_CSS,
                  cardTemplates=MODEL_TEMPLATES)
        return
    # Migrate copies of the built-in model minted before newer fields (Image,
    # Notes, Context): append the missing fields and refresh the
    # template/styling so old decks pick them up too.
    existing = anki_call("modelFieldNames", modelName=MODEL_NAME) or []
    missing = [f for f in MODEL_FIELDS if f not in existing]
    if missing:
        for i, field in enumerate(missing):
            anki_call("modelFieldAdd", modelName=MODEL_NAME,
                      fieldName=field, index=len(existing) + i)
        t = MODEL_TEMPLATES[0]
        anki_call("updateModelTemplates", model={
            "name": MODEL_NAME,
            "templates": {t["Name"]: {"Front": t["Front"], "Back": t["Back"]}},
        })
        anki_call("updateModelStyling",
                  model={"name": MODEL_NAME, "css": MODEL_CSS})


def _interest_first(conn, picks):
    """Push-time ordering, applied on every path (server close-out,
    direct-mode CLI, .apkg fallback): standing high-interest lemmas (ledger
    tap_interest minus known) jump the queue — stable, so pool order stays
    the tiebreak. Direct mode used to skip tools.select entirely and lose
    the ★-priority; ordering here catches it everywhere."""
    interest = lc.active_interest(conn)
    return sorted(picks, key=lambda p: p.get("lemma") not in interest)


def push_cards(cfg, episode_id, picks, anki_call=None, conn=None, log=print,
               on_progress=None, gate=None):
    """AnkiConnect path: store media, add notes, register in the ledger.
    gate overrides the config-resolved audio gate (tests)."""
    transcript = load_transcript(cfg, episode_id)
    deck_cfg = cfg.get("deck", {})
    deck_name = deck_cfg.get("name", "Immersion Mining")
    note_type = deck_cfg.get("note_type", MODEL_NAME)
    field_map = deck_cfg.get("field_map") or DEFAULT_FIELD_MAP
    conn = conn or lc.open_db(cfg["ledger_db"])
    picks = _interest_first(conn, picks)
    # Only grab frames when the target note type actually has an image field.
    prepared, ungossed = _prepare_clips(
        cfg, episode_id, transcript, picks, log=log,
        on_progress=on_progress,
        want_image="image" in field_map,
        gate=gate or _resolve_audio_gate(cfg, log),
        require=_required_gloss(field_map))
    anki_call = anki_call or partial(
        anki_request, url=cfg.get("anki_connect_url", "http://localhost:8765"))

    if note_type == MODEL_NAME:
        _ensure_model(anki_call)
    elif note_type not in anki_call("modelNames"):
        raise RuntimeError(
            f"note type {note_type!r} not found in Anki — check config deck.note_type")
    anki_call("createDeck", deck=deck_name)

    title = transcript["episode"].get("title", episode_id)
    minted, skipped = [], []
    for i, p in enumerate(prepared, 1):
        if on_progress:
            on_progress(f"pushing card {i}/{len(prepared)}")
        anki_call("storeMediaFile", filename=p["clip_name"], path=p["clip_path"])
        if p.get("image_name"):
            anki_call("storeMediaFile", filename=p["image_name"],
                      path=p["image_path"])
        note = {
            "deckName": deck_name,
            "modelName": note_type,
            "fields": _note_fields(field_map, p, title),
            "options": {"allowDuplicate": False},
        }
        try:
            note_id = anki_call("addNote", note=note)
        except RuntimeError as e:
            log(f"  skip {p['lemma']}: {e}")
            skipped.append(p["lemma"])
            continue
        minted.append({"lemma": p["lemma"], "sentence": p["sentence"],
                       "anki_guid": None, "anki_note_id": note_id})

    if minted:
        lc.record_mined_cards(conn, episode_id, minted)
        lc.promote(conn)
    return {"pushed": len(minted), "skipped": skipped, "deck": deck_name,
            "ungossed": ungossed}


def build_apkg(cfg, episode_id, picks, conn=None, log=print, gate=None):
    """Offline fallback: .apkg with stable guids, registered in the ledger."""
    import genanki

    transcript = load_transcript(cfg, episode_id)
    conn = conn or lc.open_db(cfg["ledger_db"])
    picks = _interest_first(conn, picks)
    # The built-in model has Notes/Context but no English field.
    prepared, ungossed = _prepare_clips(
        cfg, episode_id, transcript, picks, log=log,
        gate=gate or _resolve_audio_gate(cfg, log),
        require=("notes", "context"))
    deck_name = cfg.get("deck", {}).get("name", "Immersion Mining")
    title = transcript["episode"].get("title", episode_id)

    model = genanki.Model(
        _GENANKI_MODEL_ID, MODEL_NAME,
        fields=[{"name": f} for f in MODEL_FIELDS],
        templates=[{"name": t["Name"], "qfmt": t["Front"], "afmt": t["Back"]}
                   for t in MODEL_TEMPLATES],
        css=MODEL_CSS,
    )

    class _Note(genanki.Note):
        @property
        def guid(self):
            return genanki.guid_for(episode_id, self.fields[5])

    import hashlib
    deck_id = int.from_bytes(hashlib.sha256(deck_name.encode()).digest()[:4], "big") & 0x7FFFFFFF
    deck = genanki.Deck(deck_id, deck_name)
    media, minted = [], []
    for p in prepared:
        note = _Note(model=model, fields=[
            p.get("sentence_furigana") or p["sentence"],
            f"[sound:{p['clip_name']}]", p["lemma"],
            p["reading"], title, str(p["sentence_idx"]), p.get("image", ""),
            p.get("notes", ""), p.get("context", ""),
        ])
        deck.add_note(note)
        media.append(p["clip_path"])
        if p.get("image_path"):
            media.append(p["image_path"])
        minted.append({"lemma": p["lemma"], "sentence": p["sentence"],
                       "anki_guid": note.guid, "anki_note_id": None})

    out = episode_dir(cfg, episode_id) / "deck.apkg"
    pkg = genanki.Package(deck)
    pkg.media_files = media
    pkg.write_to_file(str(out))

    if minted:
        lc.record_mined_cards(conn, episode_id, minted)
        lc.promote(conn)
    return {"apkg": str(out), "cards": len(minted), "deck": deck_name,
            "ungossed": ungossed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("episode_id")
    ap.add_argument("picks", help="JSON file: [{lemma, sentence_idx, reading?}, ...]")
    ap.add_argument("--apkg", action="store_true",
                    help="build an .apkg instead of pushing via AnkiConnect")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    picks = read_json(args.picks)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    if args.apkg:
        result = build_apkg(cfg, args.episode_id, picks, log=log)
    else:
        result = push_cards(cfg, args.episode_id, picks, log=log)
    print(result)


if __name__ == "__main__":
    main()
