#!/usr/bin/env python3
"""pcremote — the Windows desktop over ssh, for what still needs its GPU.

Since the library moved to the media server (tools/library.py) the desktop
is only the GPU box: ASR (gpu_service, HTTP) and manga boxing (mokuro,
`gpu_service/ocr_volume.py`, run over ssh by tools/manga.py). This module is
the ssh/scp plumbing for the latter — cmd.exe on Windows OpenSSH, GNU tar
for folder streams.

    "manga": {"ocr_ssh_host": "transcribe-svc@192.168.0.230",
              "ocr_ssh_identity": "~/.ssh/transcribe_remote_ed25519", …}
"""

import os
import subprocess
from pathlib import Path


def _win(p):
    return str(p).replace("/", "\\")


class WinRemote:
    """cmd.exe on the desktop over the LAN ssh host (Windows OpenSSH)."""

    def __init__(self, host, identity, timeout=3600):
        self.host = host
        self.identity = os.path.expanduser(identity)
        self.timeout = int(timeout or 3600)

    def _base(self, prog):
        return [prog, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-i", self.identity, "-o", "IdentitiesOnly=yes"]

    def run(self, cmd, timeout=None, check=True):
        r = subprocess.run([*self._base("ssh"), self.host, cmd],
                           capture_output=True, timeout=timeout or self.timeout)
        out = r.stdout.decode("utf-8", errors="replace")
        err = r.stderr.decode("utf-8", errors="replace")
        # Windows OpenSSH's pq-kex banner lands on stderr for every call
        err = "\n".join(l for l in err.splitlines() if not l.startswith("**")).strip()
        if check and r.returncode != 0:
            raise RuntimeError(f"remote command failed ({r.returncode}): {cmd[:120]}…\n"
                               f"{(err or out).strip()[-600:]}")
        return r.returncode, out, err

    def exists(self, remote_path):
        rc, _, _ = self.run(f'if exist "{_win(remote_path)}" (exit 0) else (exit 1)',
                            check=False)
        return rc == 0

    def mkdir(self, remote_dir):
        self.run(f'if not exist "{_win(remote_dir)}" mkdir "{_win(remote_dir)}"')

    def rmtree(self, remote_dir):
        self.run(f'if exist "{_win(remote_dir)}" rmdir /s /q "{_win(remote_dir)}"')

    def scp_from(self, remote_path, local_path):
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_name(local_path.name + ".part")
        r = subprocess.run([*self._base("scp"), "-q", f"{self.host}:{remote_path}", str(tmp)],
                           capture_output=True, timeout=self.timeout)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"scp failed: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
        tmp.replace(local_path)
        return local_path


def pull_tree(remote, remote_dir, local_dir, log=print):
    """Copy a PC folder to the Mac in one ssh stream (the PC's tar → local
    tar) — one connection for 200 files instead of 200 scp handshakes.
    Falls back to scp -r when tar isn't available remotely."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    log(f"pulling {remote_dir}")
    src = subprocess.Popen([*remote._base("ssh"), remote.host,
                            f'tar -cf - -C "{_win(remote_dir)}" .'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen(["tar", "-xf", "-", "-C", str(local_dir)],
                           stdin=src.stdout, stderr=subprocess.PIPE)
    src.stdout.close()
    _, dst_err = dst.communicate(timeout=remote.timeout)
    src_err = src.communicate(timeout=60)[1]
    if src.returncode == 0 and dst.returncode == 0:
        return local_dir
    err = (src_err + dst_err).decode("utf-8", "replace")
    log(f"tar stream failed ({src.returncode}/{dst.returncode}) — falling back to scp")
    r = subprocess.run([*remote._base("scp"), "-q", "-r",
                        f"{remote.host}:{remote_dir}/.", str(local_dir)],
                       capture_output=True, timeout=remote.timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pull failed: {err.strip()[-300:]} / "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return local_dir


def push_tree(remote, local_dir, remote_dir, log=print):
    """Copy a Mac folder to the PC in one ssh stream (local tar → the PC's
    tar). Creates remote_dir. Falls back to scp -r."""
    local_dir = Path(local_dir)
    log(f"pushing {local_dir.name} → {remote_dir}")
    remote.mkdir(remote_dir)
    src = subprocess.Popen(["tar", "-cf", "-", "-C", str(local_dir), "."],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dst = subprocess.Popen([*remote._base("ssh"), remote.host,
                            f'tar -xf - -C "{_win(remote_dir)}"'],
                           stdin=src.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    src.stdout.close()
    _, dst_err = dst.communicate(timeout=remote.timeout)
    src_err = src.communicate(timeout=60)[1]
    if src.returncode == 0 and dst.returncode == 0:
        return remote_dir
    err = (src_err + dst_err).decode("utf-8", "replace")
    log(f"tar stream failed ({src.returncode}/{dst.returncode}) — falling back to scp")
    r = subprocess.run([*remote._base("scp"), "-q", "-r", f"{local_dir}/.",
                        f"{remote.host}:{remote_dir}"],
                       capture_output=True, timeout=remote.timeout)
    if r.returncode != 0:
        raise RuntimeError(f"push failed: {err.strip()[-300:]} / "
                           f"{r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return remote_dir
