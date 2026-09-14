#!/bin/bash
# Snapshot every file the game writes for one turn.
#
# ftherlnd is included deliberately. It is the host's master state and holds
# things the .trn does not — the turn's messages live there, and it carries all
# 166 provinces with no fog of war, which is what settled the recruitment queue
# fields when six turns of .trn diffing could not.
#
# It is a BUILD-TIME artefact only. It contains every nation's hidden state, so
# it is a Rosetta stone for decoding and must never be fed to the assistant at
# play time.
set -euo pipefail
NAME="${1:?usage: snapshot.sh <label>   e.g. snapshot.sh t7}"
SRC="${DOM6_SAVE:-$HOME/.dominions6/savedgames/example_game}"
DEST="knowledge/snapshots/$NAME"
mkdir -p "$DEST"
for f in "$SRC"/*.trn "$SRC"/*.2h "$SRC"/ftherlnd "$SRC"/*.map; do
  [ -e "$f" ] && cp -p "$f" "$DEST/"
done
echo "snapshot $NAME:"
ls -la "$DEST" | tail -n +2
