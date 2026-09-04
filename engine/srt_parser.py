"""SRT parsing, sentence merging, and subtitle alignment."""

import re

# Minimum ratio of punctuation marks to blocks.  Well-punctuated scrolling
# subs typically have ~0.9 marks per block; anything below this threshold
# means the file is missing enough punctuation to break sentences.
PUNCTUATION_THRESHOLD = 0.22
# Minimum 。 per block. Hand-authored subs that terminate statements at all
# run ~0.4+; streaming subs that only carry ？！ sit at 0.00.
DECLARATIVE_THRESHOLD = 0.05


def has_good_punctuation(subs, sentence_punct, logger=None):
    """Check whether subtitle blocks have enough sentence-ending punctuation.

    Looks at the combined text of all blocks (not just block endings) because
    scrolling auto-subs often have punctuation mid-block after deduplication.
    Returns True if there are at least PUNCTUATION_THRESHOLD punctuation marks
    per block across the full text.

    Args:
        subs: List of (start_sec, end_sec, text) tuples
        sentence_punct: Regex pattern string matching sentence-ending punctuation
    """
    if not subs:
        return True

    punct_re = re.compile(sentence_punct)
    all_text = ''.join(text for _, _, text in subs)
    punct_count = len(punct_re.findall(all_text))
    ratio = punct_count / len(subs)
    result = ratio >= PUNCTUATION_THRESHOLD
    # Broadcast/streaming subs (Netflix, TV rips) keep ？ and ！ but drop the
    # declarative 。 entirely — enough marks to pass the ratio, yet every
    # statement runs on into the next. A file whose declaratives are almost
    # never terminated needs the restore regardless of the ratio.
    if result and '。' in sentence_punct:
        declaratives = all_text.count('。') / len(subs)
        if declaratives < DECLARATIVE_THRESHOLD:
            result = False
    if logger:
        logger.debug("Punctuation quality check", ratio=round(ratio, 3),
                      threshold=PUNCTUATION_THRESHOLD, passed=result, block_count=len(subs))
    return result


def parse_srt(filepath, logger=None):
    """Parse an SRT file into [(start_sec, end_sec, text), ...]."""
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n', content.strip())
    subs = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        ts_line = None
        text_lines = []
        for i, line in enumerate(lines):
            if '-->' in line:
                ts_line = line.strip()
                text_lines = lines[i + 1:]
                break
        if not ts_line:
            continue

        m = re.match(
            r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
            ts_line,
        )
        if not m:
            continue

        start = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        end = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        text = ' '.join(line.strip() for line in text_lines if line.strip())
        subs.append((start, end, text))

    if logger:
        logger.debug("SRT parsed", block_count=len(subs))
    return subs


def parse_srt_content(content):
    """Parse SRT string content into [{"seq", "timestamp", "text"}, ...].

    Used by the translator to preserve SRT metadata.
    """
    blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        if lines[i].strip().isdigit():
            seq = lines[i].strip()
            i += 1

            if i < len(lines) and "-->" in lines[i]:
                timestamp = lines[i]
                i += 1

                text_lines = []
                while i < len(lines) and lines[i].strip() != "":
                    text_lines.append(lines[i])
                    i += 1

                blocks.append({
                    "seq": seq,
                    "timestamp": timestamp,
                    "text": "\n".join(text_lines) if text_lines else " ",
                })

            while i < len(lines) and lines[i].strip() == "":
                i += 1
        else:
            i += 1

    return blocks


def deduplicate_scrolling_subs(subs, logger=None):
    """Remove duplicate text from YouTube's scrolling auto-subtitle format.

    YouTube auto-generated subs use a rolling two-line display where each block
    repeats the previous block's text as a prefix, plus new text. Near-zero-duration
    echo blocks appear between real blocks. This filters the echoes and strips the
    overlapping prefixes so each block contains only its new text.

    Safe to call on normal (non-scrolling) subtitles — returns them unchanged.
    """
    if not subs:
        return subs

    input_count = len(subs)
    # Detect scrolling format by checking for micro-duration (<50ms) echo blocks
    micro_count = sum(1 for s, e, _ in subs if (e - s) < 0.05)
    if micro_count < len(subs) * 0.2:
        if logger:
            logger.debug("Dedup skipped (not scrolling format)", input_count=input_count)
        return subs

    # Filter out micro-duration echo blocks and empty text blocks
    real_blocks = [(s, e, t) for s, e, t in subs if (e - s) >= 0.05 and t.strip()]
    if not real_blocks:
        return subs

    # Strip overlapping prefix from each block
    deduped = [real_blocks[0]]
    for i in range(1, len(real_blocks)):
        start, end, text = real_blocks[i]
        prev_text = deduped[-1][2]

        # Check if current text starts with previous text (exact prefix)
        if len(text) > len(prev_text) and text[:len(prev_text)] == prev_text:
            new_text = text[len(prev_text):].strip()
            if new_text:
                deduped.append((start, end, new_text))
        else:
            # Fallback: find longest suffix of prev_text that matches a prefix
            # of text. Handles cases where the first block was malformed or the
            # overlap is partial (e.g. two-line display where only line 2 carries
            # over as line 1 of the next block).
            overlap = 0
            max_check = min(len(prev_text), len(text))
            for ol in range(max_check, 0, -1):
                if prev_text[-ol:] == text[:ol]:
                    overlap = ol
                    break

            if overlap > 0:
                new_text = text[overlap:].strip()
                if new_text:
                    deduped.append((start, end, new_text))
            else:
                deduped.append((start, end, text))

    if logger:
        logger.debug("Dedup complete", input_count=input_count, output_count=len(deduped),
                      removed=input_count - len(deduped))
    return deduped


# Pattern matching text that is entirely non-speech annotations:
# - One or more bracketed groups: [Music], (拍手), 【音楽】, （笑）, etc.
# - Music symbols: ♪ ♫ 🎵 🎶 (alone or repeated, with optional whitespace)
# - Combinations of the above
_BRACKET_GROUP = r'(?:[\[\(（【〔〈《「『].*?[\]\)）】〕〉》」』])'
_MUSIC_SYMBOLS = r'[♪♫🎵🎶～〜]+'
_NON_SPEECH_RE = re.compile(
    r'^\s*(?:' + _BRACKET_GROUP + r'|' + _MUSIC_SYMBOLS + r')(?:\s*(?:' + _BRACKET_GROUP + r'|' + _MUSIC_SYMBOLS + r'))*\s*$'
)


def _is_non_speech(text):
    """Return True if text consists entirely of non-speech annotations."""
    return bool(_NON_SPEECH_RE.match(text))


# Hand-authored (broadcast / streaming) subs carry markup the tokenizer must
# never see: Unicode bidi controls wrapping every line (Netflix: U+202A…U+202C),
# a BOM, dialogue dashes ("-（父親）もう治ったか？ -（男の子）うん"), speaker /
# sound-source tags at the start of a line ("（テレビ:キャスター）戦後…"),
# reading glosses after a name ("富士浅田(ふじあさだ)"), and continuation
# dashes ("住民によって⸺"). Stripped per segment; a block left empty is dropped.
_BIDI_CTRL_RE = re.compile('[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]')
_DIALOGUE_DASH_RE = re.compile(r'(?:^|(?<=\s))[\-‐－–—]\s*')
_ANNOTATION_RE = re.compile(r'(?:^|(?<=\s))[\(（][^\(\)（）]{1,20}[\)）]\s*')
_READING_GLOSS_RE = re.compile(
    r'(?<=[\u4e00-\u9fff々])[\(（][\u3040-\u309f\u30a0-\u30ffー]{1,12}[\)）]')
_CONTINUATION_RE = re.compile(r'[⸺―]+(?=\s|$)')


def strip_sub_markup(text):
    """One cue's text without presentation markup (see above). Pure."""
    t = _BIDI_CTRL_RE.sub('', text)
    t = _DIALOGUE_DASH_RE.sub('', t)
    t = _READING_GLOSS_RE.sub('', t)  # before tags: （遠藤(えんどう)） nests one
    t = _ANNOTATION_RE.sub('', t)
    t = _CONTINUATION_RE.sub('', t)
    return re.sub(r'\s+', ' ', t).strip()


def strip_markup(subs, logger=None):
    """strip_sub_markup over a cue list; cues emptied by the strip are dropped
    (they were pure markup — a speaker tag with nothing after it)."""
    out = []
    for s, e, t in subs:
        t2 = strip_sub_markup(t)
        if t2:
            out.append((s, e, t2))
    if logger:
        logger.debug("Markup strip", input_count=len(subs), retained=len(out))
    return out


def filter_non_speech(subs, logger=None):
    """Remove subtitle blocks that consist entirely of non-speech annotations.

    Catches bracketed annotations like [Music], [Applause], (singing), 【拍手】,
    music symbols like ♪♫🎵🎶, and combinations thereof.

    Blocks with real speech mixed with inline annotations are kept intact.
    """
    result = [(s, e, t) for s, e, t in subs if not _is_non_speech(t)]
    if logger:
        filtered = len(subs) - len(result)
        logger.debug("Non-speech filter", input_count=len(subs), retained=len(result),
                      filtered=filtered)
    return result


def merge_to_sentences(subs, sentence_enders, min_length=0, max_duration=30.0, max_chars=200, logger=None):
    """Merge consecutive subtitle blocks until a sentence boundary.

    First splits any blocks containing multiple sentences, then merges
    fragments across blocks to form complete sentences. Finally merges
    consecutive short sentences to avoid excessive fragmentation.

    Args:
        subs: List of (start_sec, end_sec, text) tuples
        sentence_enders: Regex pattern string matching sentence-ending punctuation
                         at end of line (e.g. r'[。！？」]\\s*$')
        min_length: Minimum character length for a sentence (default 15).
                    Sentences shorter than this will be merged with adjacent ones.
        max_duration: Maximum duration in seconds for a merged sentence (default 30.0).
        max_chars: Maximum character length for a merged sentence (default 200).

    Returns [(start_sec, end_sec, merged_text), ...].
    """
    if not subs:
        return []

    # First pass: split blocks that contain multiple sentences
    # Pattern matches sentence-ending punctuation anywhere in text
    sentence_punct = sentence_enders.replace(r'\s*$', '')
    split_pattern = f'({sentence_punct})'
    split_re = re.compile(split_pattern)

    fragments = []
    for start, end, text in subs:
        # Split on sentence boundaries, keeping the punctuation
        parts = split_re.split(text)
        if len(parts) == 1:
            # No sentence boundaries found, keep as-is
            fragments.append((start, end, text))
        else:
            # Reassemble parts: [text, punct, text, punct, ...]
            # Combine each text+punct pair as a sentence fragment
            duration = end - start
            time_per_char = duration / max(len(text), 1)

            char_pos = 0
            i = 0
            while i < len(parts):
                if not parts[i]:  # Skip empty strings
                    i += 1
                    continue

                # Accumulate text and its trailing punctuation
                fragment_text = parts[i]
                fragment_start = start + (char_pos * time_per_char)
                char_pos += len(parts[i])
                i += 1

                # If next part is punctuation, add it
                if i < len(parts) and parts[i] in '。！？」.!?':
                    fragment_text += parts[i]
                    char_pos += len(parts[i])
                    i += 1

                fragment_end = start + (char_pos * time_per_char)
                if fragment_text.strip():
                    fragments.append((fragment_start, fragment_end, fragment_text.strip()))

    # Second pass: merge fragments that don't end with sentence punctuation
    if not fragments:
        return []

    enders_re = re.compile(sentence_enders)
    merged = []
    buf_start = fragments[0][0]
    buf_end = fragments[0][1]
    buf_text = fragments[0][2]

    for i in range(1, len(fragments)):
        buf_duration = fragments[i][1] - buf_start
        buf_combined_len = len(buf_text) + len(fragments[i][2])

        if enders_re.search(buf_text.rstrip()) or buf_duration > max_duration or buf_combined_len > max_chars:
            merged.append((buf_start, buf_end, buf_text.strip()))
            buf_start = fragments[i][0]
            buf_end = fragments[i][1]
            buf_text = fragments[i][2]
        else:
            buf_end = fragments[i][1]
            buf_text += fragments[i][2]

    if buf_text.strip():
        merged.append((buf_start, buf_end, buf_text.strip()))

    # Third pass: merge consecutive short sentences
    if not merged or min_length <= 0:
        return merged

    final = []
    buf_start = merged[0][0]
    buf_end = merged[0][1]
    buf_text = merged[0][2]

    for i in range(1, len(merged)):
        combined_duration = merged[i][1] - buf_start
        combined_len = len(buf_text) + len(merged[i][2])

        if len(buf_text) < min_length and combined_duration <= max_duration and combined_len <= max_chars:
            buf_end = merged[i][1]
            buf_text += merged[i][2]
        else:
            final.append((buf_start, buf_end, buf_text.strip()))
            buf_start = merged[i][0]
            buf_end = merged[i][1]
            buf_text = merged[i][2]

    if buf_text.strip():
        final.append((buf_start, buf_end, buf_text.strip()))

    if logger:
        logger.debug("Merge to sentences", input_blocks=len(subs), merged_count=len(merged),
                      final_count=len(final))
    return final


def merge_by_source(en_subs, src_subs, sentence_punct):
    """Merge both subtitle tracks in lockstep, grouping by source sentence boundaries.

    Args:
        en_subs: English subtitle list [(start, end, text), ...]
        src_subs: Source language subtitle list [(start, end, text), ...]
        sentence_punct: Regex pattern string matching sentence-ending punctuation
                        (e.g. r'[。！？」]')

    Returns [(en_text, src_start, src_end), ...].
    """
    count = min(len(en_subs), len(src_subs))
    if count == 0:
        return []

    all_src_text = ' '.join(s[2] for s in src_subs[:count])
    has_punct = bool(re.search(sentence_punct, all_src_text))

    if not has_punct:
        return [(en_subs[i][2].strip(), src_subs[i][0], src_subs[i][1])
                for i in range(count) if en_subs[i][2].strip()]

    boundary_re = sentence_punct + r'\s*$'

    groups = []
    en_buf = en_subs[0][2]
    src_buf = src_subs[0][2]
    src_start = src_subs[0][0]
    src_end = src_subs[0][1]

    for i in range(1, count):
        if re.search(boundary_re, src_buf.rstrip()):
            groups.append((en_buf.strip(), src_start, src_end))
            en_buf = en_subs[i][2]
            src_buf = src_subs[i][2]
            src_start = src_subs[i][0]
            src_end = src_subs[i][1]
        else:
            en_buf += ' ' + en_subs[i][2]
            src_buf += ' ' + src_subs[i][2]
            src_end = src_subs[i][1]

    if en_buf.strip():
        groups.append((en_buf.strip(), src_start, src_end))

    return groups


def format_ts(seconds):
    """Convert seconds to SRT timestamp HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(sentences, filepath):
    """Write sentences as an SRT file."""
    with open(filepath, "w", encoding="utf-8") as f:
        for idx, (start, end, text) in enumerate(sentences, 1):
            f.write(f"{idx}\n")
            f.write(f"{format_ts(start)} --> {format_ts(end)}\n")
            f.write(f"{text}\n\n")
