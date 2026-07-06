"""Per-episode staging layout shared by the dumb tools.

Everything an episode produces lives under <work_dir>/episodes/<episode_id>/:
    transcript.json   acquire  — sentence-segmented transcript + episode meta
    sentences.srt     acquire  — same sentences as SRT (subtitle sidecar)
    coverage.json     coverage — classification, ranked candidates, exposures
    curate.json       /immerse — synopsis, {word,gloss,note} keywords, focal
                                 points, exclude junk-filter, rationales, and the
                                 taste-metadata block (genre/format/topics/
                                 difficulty_felt → ledger via record-curation)
    picks.json        /immerse — card shortlist [{lemma, sentence_idx,
                                 reading, english}] consumed by deck
    prep.html         render   — the phone prep doc
    deck.apkg         deck     — offline fallback package
    clips/            deck     — native-audio card clips
    images/           deck     — per-card video frames (skipped if no video)
"""

import json
from pathlib import Path


def episodes_root(cfg):
    return Path(cfg["work_dir"]) / "episodes"


def episode_dir(cfg, episode_id, create=False):
    d = episodes_root(cfg) / episode_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def downloads_dir(cfg):
    d = Path(cfg["work_dir"]) / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_transcript(cfg, episode_id):
    path = episode_dir(cfg, episode_id) / "transcript.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no transcript for episode {episode_id!r} — run acquire first ({path})")
    return read_json(path)


def load_coverage(cfg, episode_id):
    path = episode_dir(cfg, episode_id) / "coverage.json"
    if not path.exists():
        raise FileNotFoundError(
            f"no coverage for episode {episode_id!r} — run coverage first ({path})")
    return read_json(path)
