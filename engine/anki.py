"""Anki deck generation — subs2srs-style .apkg with Japanese sentences and audio clips.

Vendored from audioPrimeProd/src/core/anki.py. fullPipe additions:
- every note gets a deterministic guid (genanki.guid_for(video_id, seq)) so the
  ledger's cards table can match notes on resync / lapse polling;
- create_anki_deck returns the per-card records the ledger stores.
"""

import hashlib
import random
import subprocess
from pathlib import Path

import genanki

from .audio import slice_audio, MIN_SLICE_DURATION
from .srt_parser import parse_srt
from .paths import ffprobe_path, _NOWWIN

# Stable model ID — must not change across runs so Anki recognises updates.
_MODEL_ID = 1607392319

_ANKI_MODEL = genanki.Model(
    _MODEL_ID,
    "fullPipe subs2srs",
    fields=[
        {"name": "Expression"},
        {"name": "Audio"},
        {"name": "Sequence"},
    ],
    templates=[
        {
            "name": "Card 1",
            "qfmt": "{{Expression}}<br>{{Audio}}",
            "afmt": "{{FrontSide}}<hr id=answer>",
        },
    ],
)

CLIP_PAD = 0.5  # seconds of padding before and after the merged sentence span


class _StableGuidNote(genanki.Note):
    """Note whose guid is set explicitly rather than hashed from all fields."""

    def __init__(self, guid_value, **kwargs):
        super().__init__(**kwargs)
        self._stable_guid = guid_value

    @property
    def guid(self):
        return self._stable_guid


def _stable_deck_id(title):
    """Derive a deterministic deck ID from the deck title."""
    h = hashlib.sha256(title.encode()).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def create_anki_deck(srt_path, audio_path, output_apkg, deck_title,
                     work_dir, video_id, progress_callback=None, logger=None,
                     process_tracker=None):
    """Build an .apkg deck from source language sentences and audio.

    Args:
        srt_path: Path to sentence-merged SRT file
        audio_path: Path to the source MP3
        output_apkg: Path for the output .apkg file
        deck_title: Human-readable deck name
        work_dir: Directory for temporary audio clips
        video_id: Stable episode/source id (also keys the note guids)
        progress_callback: Optional callable(current, total, message)

    Returns:
        List of card records: {"seq", "text", "start", "end", "guid", "clip"}
        — what the ledger's cards table wants to persist.
    """
    subs = parse_srt(str(srt_path))
    if not subs:
        raise RuntimeError("No subtitles found in sentence SRT file")

    # Probe total audio duration for padding clamps
    _run = process_tracker.run if process_tracker else subprocess.run
    result = _run(
        [ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(audio_path)],
        capture_output=True, text=True, check=True, **_NOWWIN,
    )
    total_duration = float(result.stdout.strip())

    clips_dir = Path(work_dir) / "anki_clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    rand_suffix = random.randint(0, 9999)

    deck = genanki.Deck(_stable_deck_id(deck_title), deck_title)
    media_files = []
    cards = []
    total = len(subs)

    for i, (start, end, text) in enumerate(subs):
        if progress_callback:
            progress_callback(i, total, f"Slicing clip {i + 1}/{total}")

        clip_start = max(0.0, start - CLIP_PAD)
        clip_end = min(total_duration, end + CLIP_PAD)

        # Guard: skip degenerate clips
        if clip_end - clip_start < MIN_SLICE_DURATION:
            continue

        clip_name = f"fullPipe_{video_id}_{rand_suffix:04d}_{i:04d}.mp3"
        clip_path = clips_dir / clip_name

        if not clip_path.exists():
            slice_audio(str(audio_path), clip_start, clip_end, str(clip_path),
                        process_tracker=process_tracker)

        media_files.append(str(clip_path))

        guid = genanki.guid_for(video_id, i)
        note = _StableGuidNote(
            guid,
            model=_ANKI_MODEL,
            fields=[text, f"[sound:{clip_name}]", str(i)],
        )
        deck.add_note(note)
        cards.append({
            "seq": i,
            "text": text,
            "start": start,
            "end": end,
            "guid": guid,
            "clip": str(clip_path),
        })

    if progress_callback:
        progress_callback(total, total, "Packaging .apkg...")

    package = genanki.Package(deck)
    package.media_files = media_files
    package.write_to_file(str(output_apkg))

    if logger:
        skipped = total - len(cards)
        apkg_size = Path(str(output_apkg)).stat().st_size if Path(str(output_apkg)).exists() else 0
        logger.debug("Anki deck created", cards=len(cards), skipped=skipped,
                     apkg_size_bytes=apkg_size)

    return cards
