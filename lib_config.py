"""Per-user config + .env loading, shared by every tool and ledger verb.

config.json (gitignored) is created by copying config.example.json — the
/setup skill will eventually interview it into existence. Paths in the
config may use ~; they are expanded on load.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_PATH_KEYS = {"work_dir", "ledger_db"}
_NESTED_PATH_KEYS = {("freq", "show_graph_db"), ("freq", "leeds_fallback"),
                     ("asr", "reazonspeech_model_dir")}


def load_env(path=None):
    """Load KEY=VALUE lines from .env into os.environ (existing vars win)."""
    env_path = Path(path) if path else ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def load_config(path=None, required=True):
    """Read config.json, expand ~ in path fields, and load .env."""
    cfg_path = Path(path) if path else ROOT / "config.json"
    if not cfg_path.exists():
        if required:
            raise FileNotFoundError(
                f"{cfg_path} not found — copy config.example.json to config.json "
                "and adjust it for your setup."
            )
        return None

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    for key in _PATH_KEYS:
        if cfg.get(key):
            cfg[key] = str(Path(cfg[key]).expanduser())
    for section, key in _NESTED_PATH_KEYS:
        if cfg.get(section, {}).get(key):
            cfg[section][key] = str(Path(cfg[section][key]).expanduser())

    if not cfg.get("ledger_db"):
        cfg["ledger_db"] = str(Path(cfg.get("work_dir", "~/immersion")).expanduser() / "ledger.db")

    load_env()
    return cfg
