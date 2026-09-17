#!/usr/bin/env python3
"""library — the media server's shares, mounted on the Mac (2026-09-14).

The Raspberry Pi at 192.168.0.147 ("mediaserver") holds the Japanese
library that used to live on the desktop's drives. It exports two SMB
shares, which the Mac mounts as network drives under /Volumes:

    library   /Volumes/library   the media  (Japanese/{anime,drama,manga,…})
    t7        /Volumes/t7        scratch + the 480p stage copies (writable)

Everything under `library` is treated as irreplaceable user data: the tools
only ever read it (CLAUDE.md). `t7` is ours to write.

`config.json → library` names the server, the mount points and the roots:

    "library": {
      "server": "smb://pi@192.168.0.147",        # login in the keychain
      "mounts": {"library": "/Volumes/library", "t7": "/Volumes/t7"},
      "series_root": "/Volumes/library/Japanese",
      "manga_root":  "/Volumes/library/Japanese/manga",
      "stage_dir":   "/Volumes/t7/fullpipe_stage"   # "" = no stage tier
    }

An SMB mount drops on sleep/reboot; `ensure_mounted(cfg, path)` re-mounts
the share a path lives on (Finder's `mount volume`, which uses the keychain
password saved when the share was first mounted) before anything touches
it. Paths from the desktop era (`E:/Japanese/...`, `H:/manga/...`) in old
manifests are mapped onto the mount by `resolve(cfg, path)` so ingested
series/manga keep working without re-ingesting.
"""

import os
import re
import subprocess
from pathlib import Path

DEFAULTS = {
    "server": "smb://pi@192.168.0.147",
    "mounts": {"library": "/Volumes/library", "t7": "/Volumes/t7"},
    "series_root": "/Volumes/library/Japanese",
    "manga_root": "/Volumes/library/Japanese/manga",
    "stage_dir": "/Volumes/t7/fullpipe_stage",
    # desktop-era prefixes (old manifests, muscle memory) → the mount
    "legacy_roots": {"E:/Japanese": "/Volumes/library/Japanese",
                     "H:/manga": "/Volumes/library/Japanese/manga"},
}


def library_cfg(cfg):
    return {**DEFAULTS, **((cfg or {}).get("library") or {})}


# --- paths ----------------------------------------------------------------------

def norm(p):
    """One separator (/) for any path we get handed — old manifests carry
    Windows backslashes, the mount is POSIX."""
    return str(p).replace("\\", "/")


def resolve(cfg, p):
    """A library path as it is on the Mac: legacy `E:/…` / `H:/…` prefixes
    map onto the mount; a relative path is taken under series_root."""
    lcfg = library_cfg(cfg)
    s = norm(p).rstrip("/") or "/"
    for old, new in sorted(lcfg["legacy_roots"].items(), key=lambda kv: -len(kv[0])):
        old_n = norm(old).rstrip("/")
        if s.lower() == old_n.lower() or s.lower().startswith(old_n.lower() + "/"):
            return new.rstrip("/") + s[len(old_n):]
    if re.match(r"^[A-Za-z]:/", s):
        raise FileNotFoundError(f"{p}: a desktop drive path with no mapping in "
                                f"config.json → library.legacy_roots")
    if not s.startswith("/") and not s.startswith("~"):
        return f"{lcfg['series_root'].rstrip('/')}/{s}"
    return os.path.expanduser(s)


def name_of(p):
    return norm(p).rstrip("/").rsplit("/", 1)[-1]


def parent_of(p):
    s = norm(p).rstrip("/")
    return s.rsplit("/", 1)[0] if "/" in s else ""


def suffix_of(p):
    n = name_of(p)
    return ("." + n.rsplit(".", 1)[1].lower()) if "." in n else ""


# --- mounts ---------------------------------------------------------------------

def mount_of(cfg, path):
    """(share name, mount point) the path lives on, or None if it is not
    under any configured mount (a temp dir in tests, a local folder)."""
    s = norm(path)
    for share, mp in library_cfg(cfg)["mounts"].items():
        mp_n = norm(mp).rstrip("/")
        if s == mp_n or s.startswith(mp_n + "/"):
            return share, mp_n
    return None


def is_mounted(mount_point):
    return os.path.ismount(mount_point)


def mount_share(cfg, share, log=print):
    """Mount one share the way Finder does (keychain login, lands under
    /Volumes/<share>). Raises with a readable message when it can't."""
    lcfg = library_cfg(cfg)
    url = f"{lcfg['server'].rstrip('/')}/{share}"
    log(f"mounting {url}…")
    r = subprocess.run(["osascript", "-e", f'mount volume "{url}"'],
                       capture_output=True, text=True, timeout=90)
    mp = lcfg["mounts"][share]
    if r.returncode != 0 or not is_mounted(mp):
        raise RuntimeError(f"could not mount {url} at {mp}: "
                           f"{(r.stderr or r.stdout).strip()[-300:] or 'not mounted'} "
                           f"— is the media server (192.168.0.147) on?")
    return mp


def ensure_mounted(cfg, path, log=print):
    """Make sure the share `path` lives on is mounted; no-op for paths
    outside the configured mounts."""
    hit = mount_of(cfg, path)
    if hit and not is_mounted(hit[1]):
        mount_share(cfg, hit[0], log=log)
    return path


# --- listings -------------------------------------------------------------------

def listing(root):
    """Every regular file under root (recursive), full paths with `/`,
    AppleDouble `._x` twins and dotfiles skipped. os.walk with scandir
    batches SMB directory reads — ~30k files in a few seconds."""
    root = str(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"no such folder: {root}")
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if not f.startswith("."):
                out.append(os.path.join(d, f))
    return out


def stage_dir(cfg):
    return (library_cfg(cfg).get("stage_dir") or "").rstrip("/")


def copy_file(src, dst):
    """Atomic-ish copy: write <dst>.part beside the target, then rename."""
    import shutil
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    tmp.replace(dst)
    return dst
