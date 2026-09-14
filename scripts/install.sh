#!/usr/bin/env bash
# Set up the Dominions 6 assistant on Linux or macOS.
#
#   ./scripts/install.sh              everything: assistant + all libraries
#   ./scripts/install.sh --minimal    just the assistant, no reference libraries
#   ./scripts/install.sh --ask        choose each library individually
#   ./scripts/install.sh --start      also start the server when finished
#
# Re-running is safe: every step checks whether it has already been done.
#
# The full install includes the community wiki mirror, which takes around half
# an hour of polite scraping. That is by far the slowest part; --minimal or
# --ask skip it.
#
# Nothing here is silent about the network. The reference data comes from the
# dom6inspector project, the manual from Illwinter, transcripts from YouTube --
# each is fetched onto this machine and none is redistributed. You are told
# before each one runs.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || { echo "cannot enter $REPO" >&2; exit 1; }

# Everything, unless told otherwise: the reference libraries are the point of
# the thing, and making people opt in one at a time meant most would end up
# without them and wonder why the assistant could not cite the manual.
MODE=all
START=0
# Installing uv means running a script fetched from the internet. That is never
# implied by "install the optional libraries" -- it needs its own consent, and
# only an explicit --yes counts as giving it in advance.
UV_CONSENT=0
for arg in "$@"; do
  case "$arg" in
    --all)          MODE=all ;;
    --yes|-y)       MODE=all; UV_CONSENT=1 ;;
    --minimal)      MODE=minimal ;;
    --ask)          MODE=ask ;;
    --start)        START=1 ;;
    --help|-h)
      sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# A question only when there is a terminal to answer it. Piped into bash with
# no tty, this would otherwise read EOF and silently take the default.
ask() {
  local prompt="$1"
  case "$MODE" in
    all)     return 0 ;;
    minimal) return 1 ;;
  esac
  if [ ! -t 0 ]; then
    note "not a terminal, skipping: $prompt"
    return 1
  fi
  local reply
  read -r -p "  $prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]]
}

# Consent for the one thing that runs someone else's code from the network.
# Deliberately not routed through ask(): ask() answers yes automatically in the
# default mode, which would turn `curl | sh` into something that happens to
# you rather than something you agreed to.
confirm_uv() {
  if [ "$UV_CONSENT" = 1 ]; then
    note "(--yes given, so installing uv without asking)"
    return 0
  fi
  if [ ! -t 0 ]; then
    note "No terminal here to confirm on, so nothing was downloaded."
    return 1
  fi
  local reply
  read -r -p "  Run that now? [y/N] " reply
  [[ "$reply" =~ ^[Yy] ]]
}

# --- uv ---------------------------------------------------------------------
say "Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  # uv installs here and the current shell may not have it on PATH yet.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$candidate/uv" ] && export PATH="$candidate:$PATH"
  done
fi

if ! command -v uv >/dev/null 2>&1; then
  note "uv is not installed. It manages the Python version and dependencies."
  note "The official installer is:"
  note "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  if confirm_uv; then
    curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv install failed."
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail \
      "uv installed but is not on PATH. Open a new terminal and re-run this."
  else
    fail "uv is required. Install it with the command above, or from
  https://docs.astral.sh/uv/ -- then run this script again."
  fi
fi
note "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# --- what to install --------------------------------------------------------
EXTRAS=(--extra web --extra dev)
WANT_MANUAL=0
WANT_WIKI=0
WANT_VIDEOS=0

say "Reference libraries"
if [ "$MODE" = all ]; then
  note "Installing all of them. This is the slow part:"
  note "  - Illwinter's manual        ~18 MB, about a minute"
  note "  - video transcript fetcher  seconds; you add videos later"
  note "  - community wiki mirror     around 30 minutes"
  note ""
  note "Ctrl-C now and re-run with --minimal to skip them, or --ask to choose."
  note ""
elif [ "$MODE" = minimal ]; then
  note "Skipping all of them. Add any later; see RUNNING.md."
else
  note "The assistant works without these. Each can be added later."
fi
echo
if ask "Illwinter's manual? ~18 MB, about a minute. Searchable, cited by page."; then
  WANT_MANUAL=1; EXTRAS+=(--extra manual)
fi
if ask "Strategy video transcripts? Installs the fetcher; you add videos later."; then
  WANT_VIDEOS=1; EXTRAS+=(--extra videos)
fi
if ask "The community wiki mirror? Around 30 minutes of polite scraping."; then
  WANT_WIKI=1
fi

# --- dependencies -----------------------------------------------------------
say "Installing dependencies"
note "uv sync ${EXTRAS[*]}"
uv sync "${EXTRAS[@]}" || fail "Dependency install failed. The output above says why."
note "done"

# --- game reference data ----------------------------------------------------
say "Building the game reference database"
if [ -f knowledge/reference/reference.sqlite3 ]; then
  note "already built; refreshing"
fi
note "Fetching unit, spell, event and nation data (from the dom6inspector project)."
uv run python -m dom6_assistant.reference.build_db --refresh \
  || fail "Reference build failed. Without it the assistant cannot name anything."

# --- optional libraries -----------------------------------------------------
if [ "$WANT_MANUAL" = 1 ]; then
  say "Fetching Illwinter's manual"
  uv run dom6-assistant manual-fetch && uv run dom6-assistant manual-build \
    || note "Manual setup failed; the assistant will report it as unavailable."
fi

if [ "$WANT_WIKI" = 1 ]; then
  say "Mirroring the community wiki (this is the slow one)"
  uv run dom6-assistant wiki-scrape && uv run dom6-assistant wiki-build \
    || note "Wiki setup failed; the assistant will report it as unavailable."
fi

if [ "$WANT_VIDEOS" = 1 ]; then
  say "Video transcripts"
  note "The fetcher is installed. Add a video whenever you like:"
  note "    uv run dom6-assistant video-add <youtube url>"
  note "or paste a YouTube link into the chat and ask for it to be indexed."
fi

# --- where saves are --------------------------------------------------------
say "Your Dominions saves"
SAVE_ROOT="$(uv run python -c 'from dom6_assistant.paths import describe_save_root; print(describe_save_root())' 2>/dev/null)"
if [ -n "$SAVE_ROOT" ]; then
  note "$SAVE_ROOT"
  ROOT_ONLY="${SAVE_ROOT%% (*}"
  if [ -d "$ROOT_ONLY" ]; then
    note "found it; turns will be read automatically while the server runs"
  else
    note "not found yet. That is fine if you have not played a game."
    note "If your saves live elsewhere, set DOM6_SAVE_ROOT to that folder."
  fi
fi

# --- done -------------------------------------------------------------------
say "Ready"
note "Start it with:"
note "    uv run uvicorn dom6_assistant.ui.app:app --port 8001"
note ""
note "Then open http://127.0.0.1:8001/ and point the Model endpoint box at"
note "your model. Test connection tells you whether it worked."
echo

# Only on request, or when the questions were being asked anyway. A default
# install that ends in a blocking server would look like it had hung.
if [ "$START" = 1 ]; then
  exec uv run uvicorn dom6_assistant.ui.app:app --port 8001
elif [ "$MODE" = ask ] && ask "Start it now?"; then
  exec uv run uvicorn dom6_assistant.ui.app:app --port 8001
fi
