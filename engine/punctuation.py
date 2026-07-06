"""Punctuation restoration for subtitle text via OpenAI.

Vendored from ankiDeckMaker/src/core/punctuation.py, made self-contained:
the language registry lives here (fullPipe is Japanese-first), the debug
cache directory is a parameter, and tiktoken is optional.

The load-bearing idea (see DESIGN.md — Acquire stage): _extract_punct_insertions
diffs raw ASR text against the LLM output and keeps ONLY the punctuation the
LLM inserted, discarding every other edit. That keeps the text byte-identical
to what the ASR word timestamps describe, so downstream sentence-boundary
audio cuts land in the right place.
"""

import json
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_MODEL = "gpt-4o-mini"
MAX_OUTPUT_LIMIT = 16384
MAX_INPUT_PER_CHUNK = 10000
CJK_PUNCT_CHUNK_SIZE = 80
CJK_PUNCT_OVERLAP = 12
API_RETRY_DELAYS = (1.0, 2.0, 4.0)

# --- language config (subset of ankiDeckMaker's LANGUAGE_REGISTRY) ----------

CJK_LANGUAGES = {
    "Japanese",
    "Chinese (Simplified)",
    "Chinese (Traditional)",
    "Chinese (Taiwanese)",
    "Cantonese",
}

_CJK_PROMPT = (
    "You are a {LANGUAGE} punctuation engine.\n\n"
    "Task:\n"
    "You will receive unpunctuated {LANGUAGE} text from speech transcripts.\n"
    "Insert sentence-ending punctuation to restore natural sentence boundaries.\n\n"
    "Allowed insertions only:\n"
    "- CJK punctuation: 。 ？ ！\n"
    "- ASCII fallback: . ? !\n\n"
    "Critical rules:\n"
    "- Preserve all original characters and all line breaks exactly\n"
    "- Do NOT rewrite, delete, reorder, or replace any words\n"
    "- Only INSERT punctuation from the allowed list\n"
    "- Sentence boundaries may occur anywhere, including mid-line\n"
    "- If a boundary is uncertain, prefer inserting 。 rather than leaving long run-on text\n"
    "- Do NOT remove any existing punctuation (commas 、, existing punctuation, etc.)\n"
    "- Return only the punctuated text, nothing else\n\n"
    "Output quality target:\n"
    "- Add enough sentence boundaries to avoid long run-ons\n"
    "- Aim for frequent natural sentence endings in spoken style"
)

_GENERIC_PROMPT = (
    "You are a {LANGUAGE} punctuation engine.\n\n"
    "Task:\n"
    "You will receive unpunctuated {LANGUAGE} text from speech transcripts.\n"
    "Insert sentence-ending punctuation to restore natural sentence boundaries.\n\n"
    "Allowed insertions only:\n"
    "- . at the end of declarative sentences, ? after questions, ! after exclamations\n\n"
    "Critical rules:\n"
    "- Preserve all original characters and all line breaks exactly\n"
    "- Do NOT rewrite, delete, reorder, or replace any words\n"
    "- Only INSERT punctuation from the allowed list\n"
    "- Sentence boundaries may occur anywhere, including mid-line\n"
    "- If a boundary is uncertain, prefer inserting . rather than leaving long run-on text\n"
    "- Do NOT remove any existing punctuation (commas, existing punctuation, etc.)\n"
    "- Return only the punctuated text, nothing else\n\n"
    "Output quality target:\n"
    "- Add enough sentence boundaries to avoid long run-ons\n"
    "- Aim for frequent natural sentence endings in spoken style"
)


def get_language_config(display_name):
    """Return sentence-punctuation config for a language display name."""
    if display_name in CJK_LANGUAGES:
        return {
            "sentence_enders": r'[。！？.!?」]\s*$',
            "sentence_punct": r'[。！？.!?」]',
            "punct_chars": set('。！？.!?'),
            "punctuation_prompt": _CJK_PROMPT.replace("{LANGUAGE}", display_name),
        }
    return {
        "sentence_enders": r'[.!?]\s*$',
        "sentence_punct": r'[.!?]',
        "punct_chars": set('.!?'),
        "punctuation_prompt": _GENERIC_PROMPT.replace("{LANGUAGE}", display_name),
    }


# --- token counting (tiktoken optional) --------------------------------------

_encoder = None
_encoder_failed = False


def count_tokens(text):
    global _encoder, _encoder_failed
    if _encoder is None and not _encoder_failed:
        try:
            import tiktoken
            _encoder = tiktoken.encoding_for_model("gpt-4o-mini")
        except Exception:
            _encoder_failed = True
    if _encoder is not None:
        return len(_encoder.encode(text))
    # Heuristic fallback: CJK-heavy text runs ~1 token/char, latin ~1/4 chars.
    # Overestimating is safe (smaller chunks).
    return max(1, len(text))


# --- OpenAI plumbing ----------------------------------------------------------

def _is_retryable_api_error(exc):
    """Return True for transient OpenAI failures worth retrying."""
    from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError
    if isinstance(exc, (InternalServerError, APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code in {408, 409, 429, 500, 502, 503, 504}


def _chat_create_with_retry(client, *, model, messages, max_tokens, temperature):
    """Call chat.completions with retry on transient server/network failures."""
    for i, delay in enumerate((0.0, *API_RETRY_DELAYS)):
        if delay > 0:
            time.sleep(delay)
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            if i == len(API_RETRY_DELAYS) or not _is_retryable_api_error(exc):
                raise


# --- the crown jewel: diff-based insert-only merge ---------------------------

def _extract_punct_insertions(original, punctuated, punct_chars):
    """Use diff to find only the punctuation marks the LLM inserted.

    Compares original text against LLM output, keeping the original text
    intact but inserting any sentence-ending punctuation that was added.
    Discards all other changes (word rewrites, deletions, etc.).

    Returns the original text with only punctuation insertions applied.
    """
    matcher = SequenceMatcher(None, original, punctuated, autojunk=False)
    result = []

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == 'equal':
            result.append(original[i1:i2])
        elif op == 'insert':
            # LLM inserted characters — keep only punctuation
            inserted = punctuated[j1:j2]
            punct_only = ''.join(c for c in inserted if c in punct_chars)
            if punct_only:
                result.append(punct_only)
        elif op == 'replace':
            # LLM changed text — keep original, but extract any new punct
            orig_chunk = original[i1:i2]
            new_chunk = punctuated[j1:j2]
            # Find punct in new that wasn't in old
            new_punct = ''.join(c for c in new_chunk if c in punct_chars)
            old_punct = ''.join(c for c in orig_chunk if c in punct_chars)
            result.append(orig_chunk)
            # If LLM added more punct marks than original had, append extras
            extra = len(new_punct) - len(old_punct)
            if extra > 0:
                result.append(new_punct[-extra:])
        elif op == 'delete':
            # LLM deleted characters — keep original
            result.append(original[i1:i2])

    return ''.join(result)


def _realign_to_blocks(original_texts, merged_punctuated, punct_chars):
    """Split merged punctuated text back into per-block strings.

    Walks through the merged text character by character, consuming each
    original block's characters and absorbing any punctuation the LLM inserted.
    """
    result = []
    p = 0

    for orig in original_texts:
        block_chars = []
        o = 0

        while o < len(orig):
            if p >= len(merged_punctuated):
                block_chars.append(orig[o:])
                break

            pc = merged_punctuated[p]

            if pc in punct_chars and (o >= len(orig) or orig[o] not in punct_chars):
                block_chars.append(pc)
                p += 1
            elif pc == orig[o]:
                block_chars.append(pc)
                p += 1
                o += 1
            else:
                # Shouldn't happen after _extract_punct_insertions
                block_chars.append(orig[o])
                o += 1

        # Absorb trailing punctuation at block boundary
        while p < len(merged_punctuated) and merged_punctuated[p] in punct_chars:
            block_chars.append(merged_punctuated[p])
            p += 1

        # If we couldn't match any content from punctuated text but original had content,
        # preserve the original text to avoid losing blocks
        block_text = ''.join(block_chars)
        if not block_text.strip() and orig.strip():
            # Realignment failed for this block, keep original
            result.append(orig)
        else:
            result.append(block_text)

    return result


def _send_for_punctuation(client, text, model, punctuation_prompt):
    """Send raw text to the LLM for punctuation. Returns punctuated string."""
    input_tokens = count_tokens(text)
    max_tokens = min(max(int(input_tokens * 2), 256), MAX_OUTPUT_LIMIT)

    response = _chat_create_with_retry(
        client,
        model=model,
        messages=[
            {"role": "system", "content": punctuation_prompt},
            {"role": "user", "content": text},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )

    result = response.choices[0].message.content

    # Strip markdown code fences
    if result.startswith("```"):
        lines = result.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        result = "\n".join(lines)

    return result.strip()


def _write_punctuation_cache(cache_dir, cache_tag, chunk_number, model, source_language,
                             start_idx, end_idx, input_text, raw_output, cleaned_output):
    """Persist raw/clean punctuation outputs for postmortem debugging."""
    try:
        run_dir = Path(cache_dir) / cache_tag
        run_dir.mkdir(parents=True, exist_ok=True)

        stem = f"chunk_{chunk_number:03d}"
        (run_dir / f"{stem}_input.txt").write_text(input_text, encoding="utf-8")
        (run_dir / f"{stem}_raw_output.txt").write_text(raw_output, encoding="utf-8")
        (run_dir / f"{stem}_clean_output.txt").write_text(cleaned_output, encoding="utf-8")

        metadata = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "source_language": source_language,
            "chunk_number": chunk_number,
            "start_idx": start_idx,
            "end_idx": end_idx,
            "input_chars": len(input_text),
            "raw_output_chars": len(raw_output),
            "clean_output_chars": len(cleaned_output),
        }
        (run_dir / f"{stem}_meta.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Debug cache should never break the pipeline.
        pass


def _split_text_into_chunks(texts, max_tokens=MAX_INPUT_PER_CHUNK):
    """Split a list of texts into chunks that fit within the token limit.

    Returns list of (start_index, end_index) ranges.
    """
    chunks = []
    start = 0
    current_tokens = 0

    for i, text in enumerate(texts):
        tokens = count_tokens(text)
        if start < i and current_tokens + tokens > max_tokens:
            chunks.append((start, i))
            start = i
            current_tokens = 0
        current_tokens += tokens

    if start < len(texts):
        chunks.append((start, len(texts)))
    return chunks


def _build_overlap_chunk_specs(total_blocks, chunk_size, overlap):
    """Build expanded chunk windows plus non-overlapping core ranges.

    Returns list of (expanded_start, expanded_end, core_start, core_end).
    """
    if total_blocks <= 0:
        return []

    specs = []
    core_start = 0
    while core_start < total_blocks:
        core_end = min(core_start + chunk_size, total_blocks)
        expanded_start = max(0, core_start - overlap)
        expanded_end = min(total_blocks, core_end + overlap)
        specs.append((expanded_start, expanded_end, core_start, core_end))
        core_start = core_end

    return specs


def punctuate_subs(api_key, subs, source_language="Japanese",
                   progress_callback=None, model=DEFAULT_MODEL, cache_tag=None,
                   cache_dir=None, cjk_chunk_size=None):
    """Add sentence-ending punctuation to subtitle blocks via an LLM.

    Concatenates all block text, sends it as raw prose, then realigns
    the punctuated result back to the original blocks and timestamps.

    Args:
        api_key: OpenAI API key
        subs: List of (start_sec, end_sec, text) tuples
        source_language: Display name of source language (e.g. "Japanese")
        progress_callback: Optional callable(current_chunk, total_chunks)
        model: OpenAI model to use
        cache_tag: Optional directory tag for saving raw punctuation outputs
        cache_dir: Directory for the debug cache (required if cache_tag is set)
        cjk_chunk_size: Optional override for CJK chunk core size

    Returns:
        List of (start_sec, end_sec, text) tuples with punctuation added
    """
    if not subs:
        return subs

    from openai import OpenAI

    lang = get_language_config(source_language)
    punctuation_prompt = lang["punctuation_prompt"]
    punct_chars = lang["punct_chars"]

    client = OpenAI(api_key=api_key)
    texts = [text for _, _, text in subs]
    run_cache_tag = None
    if cache_tag and cache_dir:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_cache_tag = f"{cache_tag}_{ts}"

    # CJK punctuation benefits from smaller chunks with overlap to preserve
    # sentence context near chunk boundaries while avoiding giant prompts.
    if source_language in CJK_LANGUAGES:
        effective_chunk_size = cjk_chunk_size or CJK_PUNCT_CHUNK_SIZE
        chunk_specs = _build_overlap_chunk_specs(
            total_blocks=len(texts),
            chunk_size=effective_chunk_size,
            overlap=CJK_PUNCT_OVERLAP,
        )
    else:
        chunk_specs = [(s, e, s, e) for s, e in _split_text_into_chunks(texts)]

    punctuated_texts = list(texts)

    for i, (start_idx, end_idx, core_start, core_end) in enumerate(chunk_specs):
        if progress_callback:
            progress_callback(i, len(chunk_specs))

        chunk_texts = texts[start_idx:end_idx]
        raw_concat = ''.join(chunk_texts)
        # Send with newlines for readability
        readable = '\n'.join(chunk_texts)

        raw_punctuated = _send_for_punctuation(client, readable, model, punctuation_prompt)
        punctuated = raw_punctuated
        punctuated_flat = punctuated.replace('\n', '')

        # Extract only punctuation insertions, discard word changes
        safe_merged = _extract_punct_insertions(raw_concat, punctuated_flat, punct_chars)
        # Split back into per-block strings
        realigned = _realign_to_blocks(chunk_texts, safe_merged, punct_chars)

        # Keep only the core window from each chunk to avoid overlap duplicates.
        local_core_start = core_start - start_idx
        local_core_end = local_core_start + (core_end - core_start)
        core_realigned = realigned[local_core_start:local_core_end]
        punctuated_texts[core_start:core_end] = core_realigned

        if run_cache_tag:
            _write_punctuation_cache(
                cache_dir=cache_dir,
                cache_tag=run_cache_tag,
                chunk_number=i + 1,
                model=model,
                source_language=source_language,
                start_idx=start_idx,
                end_idx=end_idx,
                input_text=readable,
                raw_output=raw_punctuated,
                cleaned_output=punctuated,
            )

    if progress_callback:
        progress_callback(len(chunk_specs), len(chunk_specs))

    # Rebuild subs with punctuated text, preserving timestamps
    result = []
    for (start, end, _), new_text in zip(subs, punctuated_texts):
        result.append((start, end, new_text))

    return result
