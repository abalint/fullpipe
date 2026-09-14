#!/usr/bin/env python3
"""ocr_volume — OCR one manga volume folder on the desktop GPU (mokuro).

Runs on the Windows PC inside the mokuro venv (I:/transcribe/mokuro/.venv —
see gpu_service/README.md "Manga OCR"); tools/manga.py invokes it over ssh:

    python ocr_volume.py "<volume dir>" "<out dir>"

For every page image (natural-sorted) it runs mokuro's MangaPageOcr — the
comic-text-detector for speech-bubble boxes + manga-ocr for the text — and
writes <out dir>/<page stem>.json with mokuro's raw page result:

    {"version", "img_width", "img_height",
     "blocks": [{"box": [x1, y1, x2, y2], "vertical": bool, "font_size": f,
                 "lines_coords": [...], "lines": ["…", …]}, …]}

Pages already done are skipped (re-runs resume); a `_done.json` summary
marks the volume complete. The source folder is only ever read — the
originals under H:/manga are never written to. Progress lines go to stdout
(`page 12/216 016.jpg  3 blocks`) so the Mac side can narrate the job.

manga-ocr is trained on furigana-bearing manga and reads the base text
only, so a furigana edition (Dragon Ball Color) OCRs the same as one
without (Dandadan) — the Mac-side transcript never sees the rubies.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _json_default(o):
    """mokuro hands back numpy scalars/arrays inside the block dicts."""
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not serializable: {type(o).__name__}")


def natural_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def page_images(src):
    # dotfiles are never pages — macOS leaves ._NNN.jpg AppleDouble twins
    # beside scans copied from a Mac
    return sorted((p for p in Path(src).iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                   and not p.name.startswith(".")),
                  key=lambda p: natural_key(p.name))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = Path(argv[0]), Path(argv[1])
    force_cpu = "--cpu" in argv
    if not src.is_dir():
        print(f"no such folder: {src}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    pages = page_images(src)
    if not pages:
        print(f"no page images under {src}", file=sys.stderr)
        return 1

    todo = [p for p in pages if not (out / (p.stem + ".json")).exists()]
    print(f"volume {src.name}: {len(pages)} pages, {len(pages) - len(todo)} already done",
          flush=True)
    t0 = time.time()
    if todo:
        # imports are slow (torch + the two models) — only pay when needed
        from mokuro.manga_page_ocr import MangaPageOcr
        ocr = MangaPageOcr(force_cpu=force_cpu)
        for i, p in enumerate(pages):
            dest = out / (p.stem + ".json")
            if dest.exists():
                continue
            try:
                result = ocr(str(p))
            except Exception as e:  # one bad page must not sink the volume
                print(f"page {i + 1}/{len(pages)} {p.name}  FAILED: {e}", flush=True)
                result = {"version": "error", "img_width": 0, "img_height": 0,
                          "blocks": [], "error": str(e)[:300]}
            result["file"] = p.name
            tmp = dest.with_suffix(".json.part")
            tmp.write_text(json.dumps(result, ensure_ascii=False, default=_json_default),
                           encoding="utf-8")
            os.replace(tmp, dest)
            print(f"page {i + 1}/{len(pages)} {p.name}  {len(result.get('blocks', []))} blocks",
                  flush=True)
    summary = {"source": str(src), "pages": [p.name for p in pages],
               "page_count": len(pages), "elapsed": round(time.time() - t0, 1),
               "done_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "_done.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    print(f"done {len(pages)} pages in {summary['elapsed']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
