#!/usr/bin/env python3
"""deck — curated card picks → Anki cards with native audio.

Cuts the native audio clip to the MERGED sentence span (±0.5s pad) — the
resolution of the subs2srs fragment flaw (DESIGN.md — Card philosophy) —
and pushes cards live via AnkiConnect (primary: createModel/createDeck/
addNote → note ids the ledger can lapse-poll). `--apkg` is the offline
fallback (genanki, stable guids). Every minted card is registered in the
ledger (mined_card evidence + cards row), then `promote` runs.

Input picks.json — the /immerse curate output:
    [{"lemma": "縄張り", "sentence_idx": 9, "reading": "なわばり",
      "english": "optional gloss/translation"}, ...]

By default cards use the built-in "fullPipe Sentence Mining" model (created
on demand). To mint onto the user's own note type instead, set in config:

    "deck": {"name": "MinePrime",
             "note_type": "Sentence Cards",
             "field_map": {"sentence": "Sentence", "audio": "Audio",
                           "english": "English"}}

field_map keys: sentence · audio · english · image · lemma · reading ·
source · sequence — map only the fields the note type has; a custom
note_type must already exist in Anki (only the built-in model is
auto-created). The --apkg fallback always uses the built-in model (an
offline .apkg can't reuse a collection's note type).

CLI:
    python -m tools.deck EPISODE_ID picks.json [--apkg] [--config PATH]
"""

import argparse
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.audio import probe_audio_duration, slice_audio, MIN_SLICE_DURATION  # noqa: E402
from lib_config import load_config  # noqa: E402
from ledger import ledgerctl as lc  # noqa: E402
from ledger.anki_known import anki_request  # noqa: E402
from tools._staging import episode_dir, load_transcript, read_json  # noqa: E402

CLIP_PAD = 0.5
# Every card clip is loudness-normalized to this integrated loudness so review
# volume is consistent across episodes/sources. Override with config
# deck.clip_target_lufs; set it to null to disable normalization.
CLIP_TARGET_LUFS = -16.0

MODEL_NAME = "fullPipe Sentence Mining"
MODEL_FIELDS = ["Expression", "Audio", "Lemma", "Reading", "Source", "Sequence"]
MODEL_CSS = """.card {
  font-family: "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-size: 26px; text-align: center; }
.lemma { color: #4a90d9; font-size: 30px; }
.meta { font-size: 14px; color: #888; margin-top: 1em; }"""
MODEL_TEMPLATES = [{
    "Name": "Card 1",
    "Front": "{{Expression}}<br>{{Audio}}",
    "Back": ("{{FrontSide}}<hr id=answer>"
             "<div class=lemma>{{Lemma}}【{{Reading}}】</div>"
             "<div class=meta>{{Source}}</div>"),
}]

_GENANKI_MODEL_ID = 1998244353  # distinct from engine.anki's subs2srs model

# Maps pick-payload keys onto the built-in model's fields; a config
# deck.field_map replaces this when minting onto the user's own note type.
DEFAULT_FIELD_MAP = {
    "sentence": "Expression", "audio": "Audio", "lemma": "Lemma",
    "reading": "Reading", "source": "Source", "sequence": "Sequence",
}


def _note_fields(field_map, p, title):
    values = {
        "sentence": p["sentence"],
        "audio": f"[sound:{p['clip_name']}]",
        "english": p.get("english", ""),
        "image": p.get("image", ""),
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


def _prepare_clips(cfg, episode_id, transcript, picks, log=print,
                   on_progress=None):
    """Cut one native-audio clip per pick. Returns enriched pick dicts.
    on_progress(msg) narrates the per-card work (an ffmpeg encode each) for
    live consumers like the server's queue row."""
    audio = transcript["episode"]["audio"]
    total = probe_audio_duration(audio)
    if total is None:
        raise RuntimeError(f"cannot probe audio: {audio}")
    by_idx = {s["idx"]: s for s in transcript["sentences"]}
    clips_dir = episode_dir(cfg, episode_id, create=True) / "clips"
    clips_dir.mkdir(exist_ok=True)
    target_lufs = cfg.get("deck", {}).get("clip_target_lufs", CLIP_TARGET_LUFS)

    prepared = []
    for i, p in enumerate(picks, 1):
        if on_progress:
            on_progress(f"cutting clip {i}/{len(picks)}")
        sent = by_idx.get(p["sentence_idx"])
        if sent is None:
            log(f"  skip {p['lemma']}: sentence_idx {p['sentence_idx']} not in transcript")
            continue
        clip_name = f"fullPipe_{episode_id}_{p['sentence_idx']:04d}.mp3"
        _clip_sentence(audio, sent, clips_dir / clip_name, total,
                       target_lufs=target_lufs)
        prepared.append({
            **p,
            "sentence": sent["text"],
            "reading": p.get("reading", ""),
            "clip_name": clip_name,
            "clip_path": str(clips_dir / clip_name),
        })
    return prepared


def _ensure_model(anki_call):
    if MODEL_NAME in anki_call("modelNames"):
        return
    anki_call("createModel",
              modelName=MODEL_NAME,
              inOrderFields=MODEL_FIELDS,
              css=MODEL_CSS,
              cardTemplates=MODEL_TEMPLATES)


def push_cards(cfg, episode_id, picks, anki_call=None, conn=None, log=print,
               on_progress=None):
    """AnkiConnect path: store media, add notes, register in the ledger."""
    transcript = load_transcript(cfg, episode_id)
    prepared = _prepare_clips(cfg, episode_id, transcript, picks, log=log,
                              on_progress=on_progress)
    anki_call = anki_call or partial(
        anki_request, url=cfg.get("anki_connect_url", "http://localhost:8765"))
    conn = conn or lc.open_db(cfg["ledger_db"])

    deck_cfg = cfg.get("deck", {})
    deck_name = deck_cfg.get("name", "Immersion Mining")
    note_type = deck_cfg.get("note_type", MODEL_NAME)
    field_map = deck_cfg.get("field_map") or DEFAULT_FIELD_MAP
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
    return {"pushed": len(minted), "skipped": skipped, "deck": deck_name}


def build_apkg(cfg, episode_id, picks, conn=None, log=print):
    """Offline fallback: .apkg with stable guids, registered in the ledger."""
    import genanki

    transcript = load_transcript(cfg, episode_id)
    prepared = _prepare_clips(cfg, episode_id, transcript, picks, log=log)
    conn = conn or lc.open_db(cfg["ledger_db"])
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
            p["sentence"], f"[sound:{p['clip_name']}]", p["lemma"],
            p["reading"], title, str(p["sentence_idx"]),
        ])
        deck.add_note(note)
        media.append(p["clip_path"])
        minted.append({"lemma": p["lemma"], "sentence": p["sentence"],
                       "anki_guid": note.guid, "anki_note_id": None})

    out = episode_dir(cfg, episode_id) / "deck.apkg"
    pkg = genanki.Package(deck)
    pkg.media_files = media
    pkg.write_to_file(str(out))

    if minted:
        lc.record_mined_cards(conn, episode_id, minted)
        lc.promote(conn)
    return {"apkg": str(out), "cards": len(minted), "deck": deck_name}


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
