#!/usr/bin/env bash
# Start the Dominions 6 assistant.
#
#   ./start.sh              serve on port 8001
#   ./start.sh --port 9000  serve somewhere else
#   ./start.sh --open       open a browser once it is listening
#
# Stop it with Ctrl-C. While it runs it watches your save folder, so finishing
# a turn in Dominions is the whole workflow -- nothing to import by hand.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO" || { echo "cannot enter $REPO" >&2; exit 1; }

PORT=8001
OPEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --open) OPEN=1; shift ;;
    --help|-h)
      # The header block is however long it is; find its end rather than
      # hardcoding a line number, which silently truncates when it is edited.
      awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && export PATH="$candidate:$PATH"
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Run ./scripts/install.sh first." >&2
  exit 1
fi

# A missing virtualenv means the install never ran. Saying so beats letting uv
# silently build one and then fail on a missing reference database.
if [ ! -d .venv ]; then
  echo "This does not look set up yet -- there is no .venv here." >&2
  echo "Run ./scripts/install.sh first." >&2
  exit 1
fi

if [ ! -f knowledge/reference/reference.sqlite3 ]; then
  echo "Warning: the game reference database is missing, so the assistant"
  echo "cannot name units, spells or nations. Run ./scripts/install.sh to"
  echo "build it. Starting anyway."
  echo
fi

URL="http://127.0.0.1:${PORT}/"
echo "Starting the assistant on ${URL}"
echo "  Ctrl-C to stop. Turns are ingested automatically while this runs."
echo

if [ "$OPEN" = 1 ]; then
  # Wait for the port to answer before opening, so the browser does not land
  # on a connection error and have to be reloaded.
  (
    for _ in $(seq 1 40); do
      if curl -sf -o /dev/null "$URL" 2>/dev/null; then
        command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 && break
        command -v open >/dev/null && open "$URL" >/dev/null 2>&1 && break
        break
      fi
      sleep 0.5
    done
  ) &
fi

exec uv run uvicorn dom6_assistant.ui.app:app --port "$PORT"
