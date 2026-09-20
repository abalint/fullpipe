"""Blu-ray PGS (.sup) bitmap subtitles → contact sheets for an AI read → .srt.

Fansub archives (kitsunekko / jimaku) carry many older shows only as the
disc's own bitmap subtitle stream. This tool decodes the PGS display sets,
renders every cue as an image, stacks them into numbered contact sheets that
a Claude agent reads and transcribes (no tesseract), and merges the agent's
text back with the cue timings into a plain .srt — which `/series` then
picks up as a pre-placed sidecar instead of ASR-ing the audio.

    python -m tools.supocr sheets  IN.sup OUT_DIR [--per-sheet 12] [--min-ms 200]
        → OUT_DIR/cues.json   [{"idx", "start", "end", "sheet", "row"}]
          OUT_DIR/sheet_NNN.png   one row per cue, index painted at the left
    python -m tools.supocr merge   OUT_DIR OUT.srt
        → reads OUT_DIR/text_*.json  [{"idx", "text"}]  (any number of files)
          and writes the .srt; cues with no text (or text "") are dropped.
    python -m tools.supocr status  OUT_DIR
        → which cue idxs are still untranscribed.

PGS layout (Blu-ray "Presentation Graphics Stream"): a sequence of segments
`PG` · PTS(4) · DTS(4) · type(1) · size(2) · payload. A display set is
PCS (0x16, composition: which objects go where) + WDS (0x17) + PDS (0x14,
palette) + ODS (0x15, RLE bitmap, possibly split over several segments) +
END (0x80). A PCS with zero composition objects clears the screen — that is
the end of the previous cue. Only the composition timing (PTS) matters here.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PCS, WDS, PDS, ODS, END = 0x16, 0x17, 0x14, 0x15, 0x80


def _segments(data: bytes):
    pos, n = 0, len(data)
    while pos + 13 <= n:
        if data[pos:pos + 2] != b"PG":
            raise ValueError(f"bad PGS magic at {pos}")
        pts, _dts, typ, size = struct.unpack(">IIBH", data[pos + 2:pos + 13])
        pos += 13
        yield pts, typ, data[pos:pos + size]
        pos += size


def _rle_decode(buf: bytes, width: int, height: int) -> bytes:
    """PGS run-length bitmap → one palette index per pixel (row-major)."""
    out = bytearray(width * height)
    i, x, y = 0, 0, 0
    n = len(buf)
    while i < n and y < height:
        b = buf[i]; i += 1
        if b != 0:
            if x < width:
                out[y * width + x] = b
            x += 1
            continue
        c = buf[i]; i += 1
        if c == 0:                       # end of line
            x, y = 0, y + 1
            continue
        if c & 0xC0 == 0:
            run, color = c & 0x3F, 0
        elif c & 0xC0 == 0x40:
            run = ((c & 0x3F) << 8) | buf[i]; i += 1; color = 0
        elif c & 0xC0 == 0x80:
            run, color = c & 0x3F, buf[i]; i += 1
        else:
            run = ((c & 0x3F) << 8) | buf[i]; color = buf[i + 1]; i += 2
        if color:
            end = min(x + run, width)
            row = y * width
            for px in range(x, end):
                out[row + px] = color
        x += run
    return bytes(out)


def _ycbcr_to_rgb(y, cb, cr):
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return tuple(max(0, min(255, int(round(v)))) for v in (r, g, b))


def decode_cues(sup_path: Path):
    """Yield {"start","end","objects":[(x, y, PIL.Image RGBA)]} per display."""
    data = sup_path.read_bytes()
    palettes: dict[int, dict[int, tuple]] = {}
    objects: dict[int, dict] = {}          # id → {w, h, data(bytearray), need}
    comp = None                            # current PCS: {pts, palette, objs:[(id,x,y)]}
    pending = None                         # the cue currently on screen
    for pts, typ, payload in _segments(data):
        if typ == PDS:
            pid = payload[0]
            pal = palettes.setdefault(pid, {})
            for k in range(2, len(payload) - 4, 5):
                eid, yy, cr, cb, a = payload[k:k + 5]
                pal[eid] = (*_ycbcr_to_rgb(yy, cb, cr), a)
        elif typ == ODS:
            oid, _ver, flag = struct.unpack(">HBB", payload[:4])
            if flag & 0x80:                # first in sequence
                dlen = int.from_bytes(payload[4:7], "big")
                w, h = struct.unpack(">HH", payload[7:11])
                objects[oid] = {"w": w, "h": h, "data": bytearray(payload[11:]), "need": dlen - 4}
            else:
                objects[oid]["data"] += payload[4:]
        elif typ == PCS:
            _w, _h, _fr, _num, _state, pal_flag, pal_id, nobj = struct.unpack(">HHBHBBBB", payload[:11])
            objs, k = [], 11
            for _ in range(nobj):
                oid, _wid, crop, x, y = struct.unpack(">HBBHH", payload[k:k + 8])
                k += 8 + (8 if crop & 0x40 else 0)
                objs.append((oid, x, y))
            comp = {"pts": pts, "palette": pal_id, "objs": objs}
        elif typ == END and comp is not None:
            t = comp["pts"] / 90000.0
            if pending is not None:
                pending["end"] = t
                yield pending
                pending = None
            if comp["objs"]:
                pal = palettes.get(comp["palette"], {})
                imgs = []
                for oid, x, y in comp["objs"]:
                    o = objects.get(oid)
                    if not o:
                        continue
                    idx = _rle_decode(bytes(o["data"]), o["w"], o["h"])
                    img = Image.new("RGBA", (o["w"], o["h"]), (0, 0, 0, 0))
                    lut = [pal.get(i, (0, 0, 0, 0)) for i in range(256)]
                    img.putdata([lut[i] for i in idx])
                    imgs.append((x, y, img))
                if imgs:
                    pending = {"start": t, "end": None, "objects": imgs}
            comp = None
    if pending is not None:
        pending["end"] = pending["start"] + 3.0
        yield pending


def _render_cue(cue, bg=(0, 0, 0, 255)) -> Image.Image:
    """Compose the cue's objects onto a tight canvas (relative placement kept)."""
    objs = cue["objects"]
    x0 = min(x for x, _, _ in objs); y0 = min(y for _, y, _ in objs)
    x1 = max(x + im.width for x, _, im in objs); y1 = max(y + im.height for _, y, im in objs)
    canvas = Image.new("RGBA", (x1 - x0, y1 - y0), bg)
    for x, y, im in objs:
        canvas.alpha_composite(im, (x - x0, y - y0))
    return canvas


def _font(size):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Monaco.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def cmd_sheets(args):
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    cues = [c for c in decode_cues(Path(args.sup)) if (c["end"] - c["start"]) * 1000 >= args.min_ms]
    per = args.per_sheet
    meta, sheet_no = [], 0
    label_w, pad, max_w = 110, 8, 1400
    font = _font(40)
    for s in range(0, len(cues), per):
        chunk = cues[s:s + per]
        rows = []
        for c in chunk:
            im = _render_cue(c)
            if im.width > max_w:
                im = im.resize((max_w, max(1, int(im.height * max_w / im.width))), Image.LANCZOS)
            rows.append(im)
        W = label_w + max(im.width for im in rows) + pad
        H = sum(max(im.height, 48) + pad for im in rows) + pad
        sheet = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(sheet)
        y = pad
        for j, (c, im) in enumerate(zip(chunk, rows)):
            idx = s + j
            draw.text((6, y), f"#{idx}", fill=(255, 220, 0), font=font)
            sheet.paste(im.convert("RGB"), (label_w, y))
            rh = max(im.height, 48) + pad
            draw.line([(0, y + rh - pad // 2), (W, y + rh - pad // 2)], fill=(70, 70, 70), width=1)
            meta.append({"idx": idx, "start": round(c["start"], 3), "end": round(c["end"], 3),
                         "sheet": sheet_no, "row": j})
            y += rh
        sheet.save(out / f"sheet_{sheet_no:03d}.png", optimize=True)
        sheet_no += 1
    (out / "cues.json").write_text(json.dumps(meta, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(cues)} cues → {sheet_no} sheets in {out}")


def _load_text(out: Path) -> dict[int, str]:
    text: dict[int, str] = {}
    for f in sorted(out.glob("text_*.json")):
        for row in json.loads(f.read_text(encoding="utf-8")):
            text[int(row["idx"])] = (row.get("text") or "").strip()
    return text


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000); m, ms = divmod(ms, 60_000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _has_japanese(t: str) -> bool:
    return any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" or "\uff01" <= ch <= "\uff5e" for ch in t)


def build_blocks(cues, text, keep_latin=False):
    """Disc subtitles re-send the whole screen each time it changes, so a line
    that stays up while another appears is repeated in consecutive cues. Turn
    the per-cue transcriptions into one timed block per line-run: a line
    continues the previous cue's block when it was on screen there too."""
    blocks, prev_lines, prev_owner = [], [], {}
    for c in cues:
        raw = text.get(c["idx"], "")
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        owner, fresh = {}, []
        for ln in lines:
            if ln in prev_lines and ln in prev_owner and prev_owner[ln]["end"] >= c["start"] - 0.05:
                blk = prev_owner[ln]
                blk["end"] = max(blk["end"], c["end"])
                owner[ln] = blk
            else:
                fresh.append(ln)
        if fresh:
            blk = {"start": c["start"], "end": c["end"], "lines": fresh}
            blocks.append(blk)
            for ln in fresh:
                owner[ln] = blk
        prev_lines, prev_owner = lines, owner
    out = []
    for b in blocks:
        # The disc hard-wraps by width, often mid-word (立/ち退き); the SRT
        # parser would join the lines with a space and split the token, so
        # emit one line per block — Japanese needs no separator.
        t = "".join(b["lines"])
        if not keep_latin and not _has_japanese(t):
            continue
        out.append((b["start"], b["end"], t))
    return out


def cmd_merge(args):
    out = Path(args.out_dir)
    cues = json.loads((out / "cues.json").read_text(encoding="utf-8"))
    text = _load_text(out)
    missing = [c["idx"] for c in cues if c["idx"] not in text]
    if missing and not args.allow_missing:
        sys.exit(f"{len(missing)} cues untranscribed (e.g. {missing[:10]}); run status, or --allow-missing")
    blocks = build_blocks(cues, text, keep_latin=args.keep_latin)
    lines = [f"{n}\n{_ts(s)} --> {_ts(e)}\n{t}\n" for n, (s, e, t) in enumerate(blocks, 1)]
    Path(args.srt).write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(blocks)} blocks from {len(cues)} cues → {args.srt}"
          + (f" ({len(missing)} missing skipped)" if missing else ""))


def cmd_status(args):
    out = Path(args.out_dir)
    cues = json.loads((out / "cues.json").read_text(encoding="utf-8"))
    text = _load_text(out)
    missing = [c["idx"] for c in cues if c["idx"] not in text]
    sheets = sorted({c["sheet"] for c in cues if c["idx"] not in text})
    print(json.dumps({"cues": len(cues), "done": len(cues) - len(missing),
                      "missing_idx": missing, "sheets_pending": sheets}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sheets"); p.add_argument("sup"); p.add_argument("out_dir")
    p.add_argument("--per-sheet", type=int, default=12); p.add_argument("--min-ms", type=int, default=200)
    p.set_defaults(fn=cmd_sheets)
    p = sub.add_parser("merge"); p.add_argument("out_dir"); p.add_argument("srt")
    p.add_argument("--allow-missing", action="store_true")
    p.add_argument("--keep-latin", action="store_true", help="keep blocks with no Japanese text (lyrics in English)")
    p.set_defaults(fn=cmd_merge)
    p = sub.add_parser("status"); p.add_argument("out_dir"); p.set_defaults(fn=cmd_status)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
