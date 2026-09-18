#!/usr/bin/env bash
# Copy a file to the deck.
#
#   tools/deck_put.sh local/path /remote/path
#
# Same environment overrides as tools/deck.sh.
set -euo pipefail

HOST="${MIXOS_DECK_HOST:-192.168.1.22}"
USER="${MIXOS_DECK_USER:-pi}"
PASS="${MIXOS_DECK_PASS:-pi}"

exec sshpass -p "$PASS" scp \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    -o ConnectTimeout=10 \
    "$1" "$USER@$HOST:$2"
