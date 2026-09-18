#!/usr/bin/env bash
# Run a command on the deck over SSH.
#
# Driving `sshpass -p ... ssh ...` through `wsl bash -lc "..."` from PowerShell
# mangles quoting on every second command, so the arguments are taken verbatim
# here instead.
#
#   tools/deck.sh 'arecord -l'
#
# Host, user and password come from the environment so they are not baked in:
#   MIXOS_DECK_HOST (default 192.168.1.22)
#   MIXOS_DECK_USER (default pi)
#   MIXOS_DECK_PASS (default pi)
set -euo pipefail

HOST="${MIXOS_DECK_HOST:-192.168.1.22}"
USER="${MIXOS_DECK_USER:-pi}"
PASS="${MIXOS_DECK_PASS:-pi}"

exec sshpass -p "$PASS" ssh \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=10 \
    "$USER@$HOST" "$@"
