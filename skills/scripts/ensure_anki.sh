#!/usr/bin/env bash
# Ensure Anki + AnkiConnect are up and stable before a skill touches them.
#
# Adapted from the sentence-mining skill: launches Anki if it's down, waits
# for AnkiConnect to answer, and confirms it stays up (3 pings) so a one-shot
# reply during a flaky big-collection load doesn't fool us. Idempotent: a
# no-op when Anki is already healthy.
#
# Usage:  bash skills/scripts/ensure_anki.sh
# Exit 0 = AnkiConnect reachable and stable; exit 1 = give up, tell the user.
#
# Honors config.json's anki_connect_url if set (defaults to localhost:8765).

set -u

FULLPIPE="$(cd "$(dirname "$0")/../.." && pwd)"

URL="$(python3 - "$FULLPIPE" <<'PY' 2>/dev/null || true
import json, sys, pathlib
cfg = pathlib.Path(sys.argv[1]) / "config.json"
url = "http://localhost:8765"
try:
    url = json.loads(cfg.read_text()).get("anki_connect_url") or url
except Exception:
    pass
print(url)
PY
)"
URL="${URL:-http://localhost:8765}"

ping() {
  curl -s --max-time 5 "$URL" -d '{"action":"version","version":6}' 2>/dev/null | grep -q '"result"'
}

stable() {
  # 3 pings, 2s apart — survives the flaky-load one-shot reply.
  for _ in 1 2 3; do
    ping || return 1
    sleep 2
  done
  return 0
}

if ping; then
  if stable; then
    echo "AnkiConnect up and stable ($URL)"
    exit 0
  fi
  echo "AnkiConnect answered once but isn't stable (mid-load?) — waiting it out…" >&2
fi

# Down or flaky: (re)launch and wait. `open -a Anki` is a no-op if it's
# already running, so this is safe even when only AnkiConnect was the problem.
echo "AnkiConnect not reachable at $URL — launching Anki…" >&2
open -a Anki 2>/dev/null || true

# Wait up to ~3 min for AnkiConnect to come up (large collections load slowly).
for i in $(seq 1 36); do
  if ping; then
    echo "AnkiConnect responded after ~$((i*5))s — verifying stability…" >&2
    if stable; then
      echo "AnkiConnect up and stable ($URL)"
      exit 0
    fi
    echo "  not stable yet, still loading…" >&2
  fi
  sleep 5
done

echo "AnkiConnect still unreachable at $URL after launching Anki." >&2
echo "Anki may be showing a modal (sync / database check) that blocks the addon, or the addon is disabled." >&2
exit 1
