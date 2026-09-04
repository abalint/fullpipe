#!/usr/bin/env python3
"""acquire — file/URL → audio + sentence-segmented transcript (DESIGN.md — Acquire).

    source subs exist AND has_good_punctuation() → use them
    else ASR → words_to_srt → punctuation restore (diff, insert-only)
    → merge_to_sentences → complete sentences with correct audio spans

Stages transcript.json + sentences.srt under <work_dir>/episodes/<episode_id>/.
Audio stays in the shared downloads cache; transcript.json records its path.

CLI:
    python -m tools.acquire <url-or-file> [--force-transcribe] [--config PATH]
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import srt_parser as SP  # noqa: E402
from engine.downloader import download_youtube  # noqa: E402
from engine.transcriber import words_sidecar_path  # noqa: E402
from engine.local_file import (generate_local_file_id, get_local_file_metadata,  # noqa: E402
                               is_local_file, prepare_local_file)
from engine.punctuation import get_language_config, punctuate_subs  # noqa: E402
from lib_config import load_config  # noqa: E402
from tools import series as series_tool  # noqa: E402
from tools._staging import downloads_dir, episode_dir, write_json  # noqa: E402

JA = get_language_config("Japanese")


def clean_subs(subs):
    """Strip presentation markup (bidi controls, speaker tags, dialogue
    dashes), dedup scrolling auto-subs, and drop non-speech annotation blocks."""
    return SP.filter_non_speech(SP.deduplicate_scrolling_subs(SP.strip_markup(subs)))


def segment(subs, api_key=None, punct_model="gpt-4o-mini", cache_dir=None,
            cache_tag=None, restore=True, log=print):
    """Punctuation-check → optional restore → merge to complete sentences.

    Returns (sentences, punctuation_restored). Pure given its inputs — the
    only side effect is the LLM call when restoration is needed.

    restore=False skips the OpenAI restore even when a key is present: the
    caller (the /immerse skill) will punctuate via a subagent instead
    (tools.punctuate). Sentences fall back to duration/length caps until then.
    """
    restored = False
    if not SP.has_good_punctuation(subs, JA["sentence_punct"]):
        if restore and api_key:
            log(f"punctuation below threshold — restoring over {len(subs)} blocks...")
            subs = punctuate_subs(api_key, subs, source_language="Japanese",
                                  model=punct_model, cache_tag=cache_tag,
                                  cache_dir=cache_dir)
            restored = True
        elif not restore:
            log("punctuation below threshold — deferring restore to the "
                "/immerse subagent (tools.punctuate)")
        else:
            log("WARNING: poor punctuation and no OPENAI_API_KEY — sentence "
                "merging will fall back to duration/length caps")
    sentences = SP.merge_to_sentences(subs, JA["sentence_enders"])
    return sentences, restored


def acquire(source, cfg, force_transcription=False, restore_punct=True, log=print):
    """Run the full acquire stage for one source. Returns the episode record."""
    asr = cfg.get("asr", {})
    if asr.get("reazonspeech_model_dir"):
        os.environ.setdefault("FULLPIPE_REAZONSPEECH_DIR", asr["reazonspeech_model_dir"])
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY")
    gpu_url = asr.get("gpu_url") or os.environ.get("FULLPIPE_GPU_URL")
    gpu_token = asr.get("gpu_token") or os.environ.get("FULLPIPE_GPU_TOKEN")
    sub_lang = cfg.get("sub_lang", "ja")
    dl_dir = downloads_dir(cfg)
    meta = {}  # rich yt-dlp provenance (youtube only); empty for local files

    series_ref = series_tool.parse_series_source(source)
    if series_ref:
        # A box-set episode (tools.series): the 480p copy lives at the episode
        # dir already (materialized from the PC on demand), the Japanese subs
        # beside the manifest, and the id is the stable series id — not the
        # path/mtime hash, so an evict + re-fetch is still the same episode.
        kind = "series"
        slug, ep_no = series_ref
        man = series_tool.load_manifest(cfg, slug)
        ep = series_tool.find_episode(man, ep_no)
        episode_id = ep["id"]
        uploader, title = man["title"], ep["title"]
        video = series_tool.materialize(cfg, slug, ep_no, log=lambda m: log(f"  {m}"))
        mp3_path, srt_path = prepare_local_file(
            video, dl_dir, sub_lang,
            progress_callback=lambda m: log(f"  {m}"),
            force_transcription=force_transcription,
            engine_pref=asr.get("engine", "auto"),
            elevenlabs_api_key=elevenlabs_key,
            gpu_url=gpu_url, gpu_token=gpu_token,
            file_id=episode_id,
            subtitle_file=series_tool.local_subs_path(cfg, slug, ep_no),
        )
        meta = {"channel": man["title"], "channel_id": f"series:{slug}",
                "series": slug, "ep_no": ep_no, "duration": ep.get("duration")}
    elif is_local_file(source):
        kind = "local"
        episode_id = generate_local_file_id(source)
        uploader, title = get_local_file_metadata(source)
        mp3_path, srt_path = prepare_local_file(
            source, dl_dir, sub_lang,
            progress_callback=lambda m: log(f"  {m}"),
            force_transcription=force_transcription,
            engine_pref=asr.get("engine", "auto"),
            elevenlabs_api_key=elevenlabs_key,
            gpu_url=gpu_url, gpu_token=gpu_token,
        )
    else:
        kind = "youtube"
        mp3_path, srt_path = download_youtube(
            source, dl_dir, sub_lang=sub_lang,
            progress_callback=lambda m: log(f"  {m}"),
            force_transcription=force_transcription,
            engine_pref=asr.get("engine", "auto"),
            elevenlabs_api_key=elevenlabs_key,
            gpu_url=gpu_url, gpu_token=gpu_token,
        )
        episode_id = f"yt_{mp3_path.stem}"
        from engine.downloader import fetch_full_metadata
        try:
            meta = fetch_full_metadata(source)
        except RuntimeError:
            meta = {}
        uploader = meta.get("uploader") or "Unknown"
        title = meta.get("title") or mp3_path.stem

    subs = clean_subs(SP.parse_srt(str(srt_path)))
    if not subs:
        raise RuntimeError(f"no usable subtitle blocks in {srt_path}")

    ep_dir = episode_dir(cfg, episode_id, create=True)
    sentences, restored = segment(
        subs,
        api_key=os.environ.get("OPENAI_API_KEY"),
        punct_model=cfg.get("punctuation", {}).get("model", "gpt-4o-mini"),
        cache_dir=Path(cfg["work_dir"]) / ".punct_cache",
        cache_tag=episode_id,
        restore=restore_punct,
        log=log,
    )

    SP.write_srt(sentences, ep_dir / "sentences.srt")

    # Stage timed words so coverage can align per-token start times
    # (engine/word_align.py). ASR runs leave a sidecar next to the raw SRT at
    # the engine's granularity; hand-crafted subs have no sidecar, but their
    # cue spans are real timing too — stage the cleaned blocks instead.
    sidecar = words_sidecar_path(srt_path)
    if sidecar.exists():
        shutil.copyfile(sidecar, ep_dir / "words.json")
    else:
        write_json(ep_dir / "words.json", {"engine": "subs", "words": [
            {"text": t, "start": round(s, 3), "end": round(e, 3)}
            for s, e, t in subs
        ]})

    episode = {
        "id": episode_id,
        "title": title,
        "uploader": uploader,
        "source": str(source),
        "kind": kind,
        "audio": str(mp3_path),
        "raw_srt": str(srt_path),
    }
    if kind == "youtube":
        # Rescue the discarded yt-dlp dump (DESIGN.md — Taste metadata): these
        # become the episode row's provenance features. record_exposure persists
        # them; the recommender only ever reads them.
        episode.update({
            "channel": meta.get("channel") or uploader,
            "channel_id": meta.get("channel_id"),
            "duration": meta.get("duration"),
            "upload_date": meta.get("upload_date"),
            "view_count": meta.get("view_count"),
            "description": meta.get("description"),
            "tags": meta.get("tags") or [],
        })
    elif kind == "series":
        # The series is the "channel" (taste groups by show), plus the
        # playlist identity the ledger keeps for rewatch history.
        episode.update(meta)
    record = {
        "episode": episode,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "punctuation_restored": restored,
        "sentences": [
            {"idx": i, "start": round(s, 3), "end": round(e, 3), "text": t}
            for i, (s, e, t) in enumerate(sentences)
        ],
    }
    write_json(ep_dir / "transcript.json", record)
    log(f"staged {len(sentences)} sentences → {ep_dir}")
    return record


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="local media file or yt-dlp compatible URL")
    ap.add_argument("--force-transcribe", action="store_true",
                    help="skip subtitle download/discovery, always ASR")
    ap.add_argument("--no-punct-restore", action="store_true",
                    help="skip the OpenAI punctuation restore; leave it to the "
                         "/immerse subagent (tools.punctuate)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    record = acquire(args.source, cfg, force_transcription=args.force_transcribe,
                     restore_punct=not args.no_punct_restore,
                     log=lambda m: print(m, file=sys.stderr))
    print(record["episode"]["id"])


if __name__ == "__main__":
    main()
