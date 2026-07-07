"""Align sentence text back to the raw ASR word stream to time tokens.

Text and timing part ways early in the pipeline: ASR words are collapsed into
SRT blocks (transcriber.words_to_srt), blocks are merged into sentences
(srt_parser.merge_to_sentences), and coverage re-tokenizes sentence *text*
with Sudachi — so by the time tokens exist, per-word timing is gone. This
module reconnects them after the fact: build a timeline that gives every
content character of the ASR stream a timestamp (char-interpolated inside
each word's span), difflib-align that character stream against the sentence
texts, and read each token's start time off the alignment.

The alignment tolerates the edits made between ASR and sentences —
punctuation restoration inserts marks (punctuation isn't a content char),
cleaning drops whole blocks (an unmatched gap), ASR quirks substitute the
odd character (interpolated across). When the streams disagree too much to
trust (< MIN_MATCH of sentence chars matched — e.g. a stale sidecar from a
different transcription), alignment returns None and callers omit timing.

Granularity is whatever the engine recorded in words.json: true words
(ElevenLabs), subword groups (ReazonSpeech), or whole Whisper segments
(GPU/Kotoba — word_timestamps hangs on distil models, so segments it is).
Coarser granularity just means more char-interpolation between anchors.
"""

import unicodedata
from difflib import SequenceMatcher

# Below this fraction of sentence content chars matched, the word stream and
# the transcript are talking about different audio — refuse to guess.
MIN_MATCH = 0.8


def is_content_char(ch):
    """Letters and numbers only — punctuation/space/symbols never align."""
    return unicodedata.category(ch)[0] in ("L", "N")


def char_timeline(words):
    """ASR words → [(content_char, seconds)] for the whole episode, in order.

    Times interpolate char-proportionally inside each word's start→end span,
    so a Whisper segment's characters spread across the segment while a true
    word's characters all land within a few hundred ms.
    """
    out = []
    for w in words or []:
        chars = [ch for ch in str(w.get("text", "")) if is_content_char(ch)]
        if not chars:
            continue
        start = float(w.get("start", 0.0))
        span = max(float(w.get("end", start)) - start, 0.0)
        out.extend(
            (ch, start + span * i / len(chars)) for i, ch in enumerate(chars)
        )
    return out


def _fill_gaps(times):
    """Interpolate None runs between matched neighbors (edge runs clamp to the
    nearest match) and enforce monotonic non-decreasing order, in place."""
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        return
    for i in range(known[0]):
        times[i] = times[known[0]]
    for i in range(known[-1] + 1, len(times)):
        times[i] = times[known[-1]]
    for a, b in zip(known, known[1:]):
        for i in range(a + 1, b):
            times[i] = times[a] + (times[b] - times[a]) * (i - a) / (b - a)
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            times[i] = times[i - 1]


def sentence_char_times(sentence_texts, timeline):
    """Per sentence, one seconds value per *content char* of its text — or
    None when the streams disagree too much to trust (see MIN_MATCH).

    sentence_texts: the transcript's sentence strings, in episode order.
    timeline: char_timeline() output for the same episode's ASR words.
    """
    owner = []  # sentence index of each content char, across the episode
    chars = []
    for i, text in enumerate(sentence_texts):
        for ch in text:
            if is_content_char(ch):
                owner.append(i)
                chars.append(ch)
    if not chars or not timeline:
        return None

    sm = SequenceMatcher(None, "".join(ch for ch, _ in timeline),
                         "".join(chars), autojunk=False)
    times = [None] * len(chars)
    matched = 0
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            times[blk.b + k] = timeline[blk.a + k][1]
        matched += blk.size
    if matched / len(chars) < MIN_MATCH:
        return None
    _fill_gaps(times)

    per_sentence = [[] for _ in sentence_texts]
    for i, t in zip(owner, times):
        per_sentence[i].append(t)
    return per_sentence
