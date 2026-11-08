"""Per-episode staging layout shared by the dumb tools.

Everything an episode produces lives under <work_dir>/episodes/<episode_id>/:
    transcript.json   acquire  — sentence-segmented transcript + episode meta
    sentences.srt     acquire  — same sentences as SRT (subtitle sidecar)
    words.json        acquire  — timed words {engine, words:[{text,start,end}]}
                                 (raw ASR words, or the sub cues for hand-sub
                                 episodes); coverage aligns per-token times
                                 from it (engine/word_align.py)
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
    # expanduser defensively: a raw config.json (not lib_config.load_config)
    # carries a literal "~", which would create ./~/immersion in the cwd
    return Path(cfg["work_dir"]).expanduser() / "episodes"


def episode_dir(cfg, episode_id, create=False):
    d = episodes_root(cfg) / episode_id
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def video_file(cfg, episode_id):
    """The episode's 480p video: <episode_dir>/video.mp4 when the Mac holds
    it, else — for a series episode — the stage copy on the media server's
    t7 share, which is where series videos live (series.video_on_server).
    None when neither is there (or the share is down)."""
    local = episode_dir(cfg, episode_id) / "video.mp4"
    if local.exists():
        return local
    if str(episode_id).startswith("ser_"):
        from tools import series as series_tool  # lazy: series imports this module
        if series_tool.video_on_server(cfg):
            return series_tool.stage_video(cfg, episode_id)
    return None


def downloads_dir(cfg):
    d = Path(cfg["work_dir"]).expanduser() / "downloads"
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
