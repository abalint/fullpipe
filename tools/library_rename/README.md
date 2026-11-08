# Normalizing episode filenames on the library share

First run: 2026-11-07, `Japanese/drama` (2,400 files renamed, 0 errors). Target
pattern is the one in `anime/retro/Hades Project Zeorymer (1988-1990)`:

    <Show> - NN[ - Episode Title].ext           single-season folder
    <Show> - S01E01[ - Episode Title].ext       multi-season show (season subfolders)
    <Show> - SP | SP1 | SP - <name> | Movie - <name>
    subtitle = its video's new stem + language tag:  Hot Spot - 01.Jpn.srt

Every new name parses back to the right episode in `tools.series.parse_episode`,
so `/series ingest` on a show's top folder gives one ordered playlist.

## The library is read-only by default

CLAUDE.md: never write to `/Volumes/library`. Renaming needs the user's
explicit OK **for that run**, and the OK covers renames only (no deletes, no
moves out of the show folder). Dry run first, show the list, then apply.

## Procedure

1. **Look first.** `find <root> -maxdepth 3 | sort` into the scratchpad;
   sample one video name per show folder; note season subfolders,
   `Subs/` folders, specials, loose films at the root (left alone).
2. **Write an overrides file** (copy `overrides.drama.json`):
   - `titles`: folder → show title, where the folder name is not the title
     (English alt titles, release-group batch names, `hotspot` → `Hot Spot`).
     Default = folder name minus `(year)` / `[No_Subs]` / a leading `[group]`.
   - `dirs`: season / specials subfolders → `[show, season#]`,
     `[show, "SP"]`, `[show, "Movie - Suspect X"]`, or `[show, "file"]`
     (take SxxEyy from the file names, e.g. Terrace House).
   - `skip`: files/folders to leave untouched (single films, a stray
     subtitle from another season sitting in the wrong folder).
   - `files`: exact new stem for one-offs (byte-identical duplicates →
     `(duplicate)`, split parts → `05 - Part 1`, movies with no number,
     broken numbering like Terrace House S04 where every file says E01).
3. **Plan** (dry run, never touches the share):
   `python3 tools/library_rename/plan.py <root> <overrides.json> <scratch>/`
   → `plan.tsv` (old_rel · new_name · note). Iterate until the summary says
   `0 unparsed; 0 colliding`. Notes: `alt-sub` = a second subtitle release
   for the same episode (the one closest to the video's release keeps the
   clean name, the other gets `(alt)`), `no-video` = a subtitle with no
   matching video (named from its own parse).
4. **Review**: per-show summary (title, episode range, titled/special
   entries) and spot-check subtitle pairs. Things that bit in the drama run:
   release-group tails read as titles (`-DoA`, `-ER`, `AOZ`, `PPX` — add to
   `GROUP_TAIL`), `_480p` read as an episode number, `e10end`, `SP1/SP2`,
   `Ep 11 … Special` (an episode, not a special), NFKC turning `？` into an
   ASCII `?` (SMB-illegal; the planner maps `?*:"<>|/\` to full-width),
   TVer `【解説放送版】` audio-description rips (kept as `(解説放送版)` —
   a show can have both versions of one episode).
5. **Verify parse**: for every new name, `tools.series.parse_episode` must
   return the season/episode in the name (see the check in the 2026-11-07
   session; specials returning None is expected — they stay unparsed for
   `/series`).
6. **References**: the only stores that hold library paths are
   `~/immersion/series/*/series.json` (`remote_video`, `remote_subs`;
   desktop-era ones use `E:\Japanese\…`). apply.py rewrites them. Grep small
   metadata files under `~/immersion` and `/Volumes/t7/fullpipe_stage` for
   the root path to be sure nothing else appeared (a recursive grep over the
   videos takes forever — restrict to json/db/txt/tsv).
7. **Apply** (only after the user says go):
   `python3 tools/library_rename/apply.py <root> <scratch>/plan.tsv <name>`
   — renames in place (case-only renames go through a temp name: the share
   is case-insensitive), refuses to overwrite, logs `new\told` to
   `~/immersion/library_rename_log/<name>.tsv`, updates manifests (backup
   `series.json.pre-rename.bak`).
8. **Check**: every planned name exists; no `*.renaming` leftovers; every
   manifest ref exists; `python -m tools.series scan "<folder>"` on a few
   shows (single-season, multi-season, one with subs); re-running plan.py
   proposes 0 renames.

Undo = read the log backwards and `os.rename(new, old)`.
