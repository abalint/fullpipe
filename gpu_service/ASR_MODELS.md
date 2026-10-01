# Japanese ASR model survey — 2026-10-01

Status: **research done, nothing tested yet.** We're still on large-v3-turbo.
This file is the handoff for the next step: a three-way test on our own audio.

## Where we are

- **In production:** `deepdml/faster-whisper-large-v3-turbo-ct2` on faster-whisper
  (CTranslate2), RTX 2070S, set by `FULLPIPE_ASR_MODEL` in `run_service.bat`.
  (The code's built-in default is still kotoba; the .bat overrides it.)
- **July 2026 test** (`I:\transcribe\eval\exp.py` on the desktop): 6 videos of
  the kind we actually watch, about 1 h of audio, CER scored against
  creator-uploaded subs.
  - turbo: 4–14 %
  - YouTube auto-captions: 10–12 %
  - Kotoba-Whisper v2.0: 26–47 % (silently dropped whole sentences under
    background music)
- **Settings that matter:** `condition_on_previous_text=False`,
  `word_timestamps` off, `FULLPIPE_ASR_NO_REPEAT_NGRAM=0`. Speed is about 16×
  real time.

## Why Qwen3-ASR was never tried

Nothing on record says it was rejected; it was simply never in the July test.
The service runs on faster-whisper, which only loads Whisper-family models.
Qwen3-ASR needs a separate transformers/vLLM backend, plus a second model for
timestamps. The README and `service.py` used to say "swap to a Qwen3-ASR CT2
build". That was wrong; no such build exists.

## Candidates

| Model | Released | Japanese evidence | Notes for our setup |
|---|---|---|---|
| **Qwen3-ASR-1.7B** + `Qwen3-ForcedAligner-0.6B` | Jan 2026 | Top of the only media-audio benchmark: 14.0 % CER vs turbo 18.4 % (Neosophie, 20 clips of news, variety, drama and anime) | Gives no timestamps by itself; the aligner adds word/character timestamps, possibly better than our segment-level ones. Max 20 min per pass, so chunking drops from 1 h to 20 min. About 3× slower than turbo (~5× real time). Use fp16, not bf16 (Turing). Use transformers rather than vLLM on the 2070S. |
| **MOSS-Transcribe-Diarize 0.9B** | Jul 2026 | Trained on Japanese; won the Interspeech 2026 conversational-speech challenge (MLC-SLM), which includes Japanese. No Japanese CER published. | Up to 90 min per pass. Returns timestamps **and speaker labels**, e.g. `[0.48][S01]text[1.66]`. Apache 2.0, small enough for 8 GB. Speaker labels could fix the punctuation gate's "…" merges across speakers and help the presenter fingerprint. |
| LFM2.5-Audio-1.5B-JP (Liquid AI) | Jun 2026 | 4.4 % CER on Common Voice vs 8.5 % for large-v3 | Built for on-device voice assistants; no results on noisy media. Optional third candidate. |

## Ruled out (and why)

- **Kotoba-Whisper v2.x:** failed our own test; last place (49.5 %) in Neosophie's too.
- **Cohere Transcribe (Mar 2026):** best on read speech in HEROZ's test, but
  29.7 % CER on media audio.
- **Granite Speech 4.1 2B / 4.0 1B (IBM):** 26–34 % on media audio; 4.0 falls
  into repetition loops on noise.
- **ARK-ASR-3B (May 2026):** 30-second limit, no timestamps, needs bf16, about
  4B parameters (won't fit). No Japanese numbers published.
- **Parakeet, ReazonSpeech nemo/k2:** fast but 32–45 % CER on media.
- **Voxtral mini:** smooths text for readability instead of transcribing
  exactly; sometimes outputs the wrong script.
- **Third Door STT (Aug 2026):** commercial, no downloadable weights; tested on
  clean read speech (JSUT) only.
- **neosophie/Qwen3-ASR-1.7B-JA:** fine-tuned for IT vocabulary, not our domain.

There is no Qwen3-ASR successor, no Kotoba v3 and no ReazonSpeech v3 as of this
date.

## Caveats

- Neosophie's media benchmark is from April. Everything published later tests
  read speech, Common Voice or English only, so our own test is the only
  evidence that counts.
- HEROZ saw turbo loop on one noisy clip (5,525 % CER). We run with
  no-repeat-ngram=0, so we're exposed to that; count looping clips separately
  when scoring.

## Next step: the three-way test

1. Turn the PC on; `ssh transcribe`. Work in `I:\transcribe\` only: the
   account can't write anywhere else.
2. Make a separate venv for the new models so faster-whisper is untouched,
   e.g. `I:\transcribe\venv-qwen`. Use transformers with fp16.
3. Run turbo, Qwen3-ASR-1.7B + aligner, and MOSS-Transcribe-Diarize on the same
   6 videos `exp.py` used in July. Score CER against creator subs, and look
   specifically for dropped sentences under background music (what sank
   Kotoba), repetition loops, and speed.
4. Accept a model only if it wins clearly on dropped sentences and CER, not
   just on the average. To adopt it, add a backend to `service.py` that returns
   the same `{"words":[{text,start,end}],...}` shape, so `words_to_srt` and the
   Mac side don't change.

## Sources

- Neosophie media benchmark (Feb, updated Apr 2026): https://neosophie.com/ja/blog/20260226-japanese-asr-benchmark
- HEROZ 11-model comparison (Aug 2026): https://techblog.heroz.jp/entry/2026/08/18/120000
- Qwen3-ASR: https://github.com/QwenLM/Qwen3-ASR (report: arXiv 2601.21337)
- MOSS-Transcribe-Diarize: https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize
- LFM2.5-Audio-JP: https://note.com/yasuda_forceai/n/nf7883326d95f
- ARK-ASR-3B: https://huggingface.co/Edge0/ARK-ASR-3B
- Open ASR model roundup (Jul 2026): https://www.marktechpost.com/2026/07/23/best-open-speech-recognition-asr-models-in-2026-wer-languages-latency-and-license-compared/ (its table wrongly says Qwen3-ASR has no Japanese)
